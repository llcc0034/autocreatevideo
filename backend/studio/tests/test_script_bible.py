from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient, APIRequestFactory

from studio.models import CharacterBible, Episode, PropBible, SceneBible, SeriesProject
from studio.serializers import CharacterBibleSerializer
from studio.services.script_bible import (
    SCRIPT_BIBLE_BEGIN_MARKER,
    SCRIPT_BIBLE_END_MARKER,
    ScriptBibleParseError,
    _valid_reviewed_provenance_sha256,
    parse_script_bible,
    render_script_bible,
    script_bible_reviewed_content_sha256,
)
from studio.views import CharacterBibleViewSet


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
    body = (
        "第1集：雨夜相遇\n" if heading else ""
    ) + (
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


class ScriptBibleAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="script-bible-owner", password="password",
        )
        self.other = get_user_model().objects.create_user(
            username="script-bible-other", password="password",
        )
        self.project = SeriesProject.objects.create(
            owner=self.user,
            title="雨夜大学",
            master_script=master_script(),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @property
    def candidates_url(self):
        return f"/api/v1/projects/{self.project.pk}/script-bible-candidates/"

    @property
    def apply_url(self):
        return f"/api/v1/projects/{self.project.pk}/script-bible-candidates/apply/"

    @property
    def breakdown_url(self):
        return f"/api/v1/projects/{self.project.pk}/breakdown-script/"

    def _candidate_response(self):
        response = self.client.get(self.candidates_url)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    @staticmethod
    def _selection(candidate, **overrides):
        value = {
            "candidate_id": candidate["candidate_id"],
            "reviewed": True,
            "name": candidate["name"],
            "description": candidate["description"],
            "visual_identity_en": candidate["visual_identity_en"],
        }
        value.update(overrides)
        return value

    def _apply(self, candidates, *, fingerprint, overrides_by_name=None):
        overrides_by_name = overrides_by_name or {}
        return self.client.post(self.apply_url, {
            "source_fingerprint": fingerprint,
            "selections": [
                self._selection(
                    candidate,
                    **overrides_by_name.get(candidate["name"], {}),
                )
                for candidate in candidates
            ],
        }, format="json")

    def test_get_is_transient_owned_and_reports_evidence_without_writes(self):
        response = self._candidate_response()

        self.assertEqual(response["schema_version"], 1)
        self.assertTrue(response["has_block"])
        self.assertEqual(len(response["candidates"]), 3)
        self.assertTrue(all(item["recommended"] for item in response["candidates"]))
        self.assertTrue(all(item["existing"] is False for item in response["candidates"]))
        self.assertTrue(all(item["confirmed"] is False for item in response["candidates"]))
        self.assertEqual(CharacterBible.objects.count(), 0)
        self.assertEqual(SceneBible.objects.count(), 0)
        self.assertEqual(PropBible.objects.count(), 0)

        other_client = APIClient()
        other_client.force_authenticate(self.other)
        hidden = other_client.get(self.candidates_url)
        self.assertEqual(hidden.status_code, 404)

    def test_reviewed_edits_are_persisted_with_provenance_and_replay_is_idempotent(self):
        initial = self._candidate_response()
        character = next(
            item for item in initial["candidates"]
            if item["entity_type"] == "character"
        )
        overrides = {
            "王强": {
                "name": "王强（校订）",
                "description": "人工确认后的新闻社学生",
                "visual_identity_en": "reviewed college reporter in a navy jacket",
            },
        }

        applied = self._apply(
            [character],
            fingerprint=initial["source_fingerprint"],
            overrides_by_name=overrides,
        )

        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertEqual(applied.data["counts"], {
            "requested": 1, "created": 1, "already_exists": 0,
        })
        self.assertIn("updated_at", applied.data)
        self.assertEqual(len(applied.data["created"]["characters"]), 1)
        saved = CharacterBible.objects.get()
        self.assertEqual(saved.name, "王强（校订）")
        self.assertEqual(saved.stable_speaker_id, "S1")
        self.assertRegex(saved.slug, r"^character-[0-9a-f]{12}$")
        provenance = saved.metadata["script_bible_v1"]
        self.assertEqual(provenance["candidate_id"], character["candidate_id"])
        self.assertEqual(provenance["reviewed_snapshot"]["name"], "王强（校订）")
        self.assertRegex(provenance["reviewed_content_sha256"], r"^[0-9a-f]{64}$")

        refreshed = self._candidate_response()
        refreshed_character = next(
            item for item in refreshed["candidates"]
            if item["candidate_id"] == character["candidate_id"]
        )
        self.assertTrue(refreshed_character["existing"])
        self.assertTrue(refreshed_character["confirmed"])
        self.assertEqual(refreshed_character["matched_bible_id"], str(saved.pk))

        replay = self._apply(
            [character],
            fingerprint=initial["source_fingerprint"],
            overrides_by_name=overrides,
        )
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["counts"]["already_exists"], 1)
        self.assertEqual(CharacterBible.objects.count(), 1)

        changed = self._apply(
            [character],
            fingerprint=initial["source_fingerprint"],
            overrides_by_name={
                "王强": {**overrides["王强"], "description": "另一份人工内容"},
            },
        )
        self.assertEqual(changed.status_code, 409, changed.data)
        changed_candidate = changed.data["error"]["details"]["candidates"][0]
        self.assertFalse(changed_candidate["confirmed"])
        self.assertEqual(
            changed_candidate["conflict_code"],
            "script_bible_reviewed_content_conflict",
        )
        saved.refresh_from_db()
        self.assertEqual(saved.description, "人工确认后的新闻社学生")

    def test_stale_and_nfkc_or_existing_conflicts_roll_back_the_whole_batch(self):
        initial = self._candidate_response()
        stale = self._apply(
            initial["candidates"], fingerprint="0" * 64,
        )
        self.assertEqual(stale.status_code, 409, stale.data)
        self.assertEqual(CharacterBible.objects.count(), 0)

        PropBible.objects.create(
            project=self.project, slug="manual-wang", name="王强",
            description="人工道具", visual_identity="manual prop",
        )
        conflict = self._apply(
            initial["candidates"], fingerprint=initial["source_fingerprint"],
        )
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertEqual(CharacterBible.objects.count(), 0)
        self.assertEqual(SceneBible.objects.count(), 0)
        self.assertEqual(PropBible.objects.count(), 1)

        self.project.props.all().delete()
        first, second = initial["candidates"][:2]
        nfkc = self.client.post(self.apply_url, {
            "source_fingerprint": initial["source_fingerprint"],
            "selections": [
                self._selection(first, name="Ａ"),
                self._selection(second, name="A"),
            ],
        }, format="json")
        self.assertEqual(nfkc.status_code, 400, nfkc.data)
        self.assertEqual(CharacterBible.objects.count(), 0)
        self.assertEqual(SceneBible.objects.count(), 0)

    def test_apply_rejects_coerced_review_and_leading_control_characters(self):
        initial = self._candidate_response()
        candidate = initial["candidates"][0]
        coerced = self._selection(candidate)
        coerced["reviewed"] = 1
        response = self.client.post(self.apply_url, {
            "source_fingerprint": initial["source_fingerprint"],
            "selections": [coerced],
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(set(response.data), {"error"})
        self.assertEqual(set(response.data["error"]), {
            "status_code", "code", "details",
        })
        self.assertEqual(response.data["error"]["status_code"], 400)
        self.assertEqual(response.data["error"]["code"], "invalid")
        self.assertIn("selections", response.data["error"]["details"])

        controlled = self._selection(candidate, name="\t王强")
        response = self.client.post(self.apply_url, {
            "source_fingerprint": initial["source_fingerprint"],
            "selections": [controlled],
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(CharacterBible.objects.count(), 0)

    def test_breakdown_validation_uses_the_global_error_envelope(self):
        response = self.client.post(self.breakdown_url, {
            "episode_count_hint": 0,
        }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["status_code"], 400)
        self.assertEqual(response.data["error"]["code"], "invalid")
        self.assertEqual(
            set(response.data["error"]),
            {"status_code", "code", "details"},
        )
        self.assertIn("episode_count_hint", response.data["error"]["details"])

    def test_breakdown_requires_confirmation_can_skip_and_preserves_full_master(self):
        blocked = self.client.post(self.breakdown_url, {}, format="json")
        self.assertEqual(blocked.status_code, 409, blocked.data)
        self.assertEqual(
            blocked.data["error"]["code"], "script_bible_confirmation_required",
        )
        self.assertEqual(Episode.objects.count(), 0)

        skipped = self.client.post(self.breakdown_url, {
            "continue_without_script_bible": True,
        }, format="json")
        self.assertEqual(skipped.status_code, 201, skipped.data)
        self.project.refresh_from_db()
        self.assertIn(SCRIPT_BIBLE_BEGIN_MARKER, self.project.master_script)
        episode = self.project.episodes.get()
        self.assertNotIn("SCRIPT_BIBLE_V1", episode.script)
        self.assertTrue(any("跳过" in warning for warning in skipped.data["warnings"]))
        self.assertEqual(CharacterBible.objects.count(), 0)

    def test_matching_manual_bible_still_requires_apply_provenance(self):
        one_character = {
            "characters": [bible_ir()["characters"][0]],
            "scenes": [],
            "props": [],
        }
        self.project.master_script = (
            f"{render_script_bible(one_character)}\n\n"
            "第1集：手工记录\n镜头1【时长=5秒】：王强进入。"
        )
        self.project.save(update_fields=("master_script", "updated_at"))
        CharacterBible.objects.create(
            project=self.project,
            slug="manual-wang-qiang",
            name="王强",
            stable_speaker_id="S1",
            description="大学新闻社学生",
            visual_identity="college reporter in a navy jacket",
        )

        candidates = self._candidate_response()
        candidate = candidates["candidates"][0]
        self.assertTrue(candidate["existing"])
        self.assertFalse(candidate["confirmed"])
        blocked = self.client.post(self.breakdown_url, {}, format="json")
        self.assertEqual(blocked.status_code, 409, blocked.data)

        applied = self._apply(
            [candidate], fingerprint=candidates["source_fingerprint"],
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertEqual(applied.data["counts"]["already_exists"], 1)
        allowed = self.client.post(self.breakdown_url, {}, format="json")
        self.assertEqual(allowed.status_code, 201, allowed.data)

    def test_breakdown_reloads_locked_master_script_when_request_omits_script(self):
        stale_project = SeriesProject.objects.get(pk=self.project.pk)
        fresh_script = "第1集：最新母稿\n镜头1【时长=5秒】：空走廊。"
        SeriesProject.objects.filter(pk=self.project.pk).update(
            master_script=fresh_script,
        )

        with patch(
            "studio.views.SeriesProjectViewSet.get_object",
            return_value=stale_project,
        ):
            response = self.client.post(self.breakdown_url, {}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        episode = Episode.objects.get(project=self.project)
        self.assertEqual(episode.title, "最新母稿")
        self.assertIn("空走廊", episode.script)
        self.assertNotIn("王强", episode.script)
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, fresh_script)

    def test_apply_then_breakdown_binds_entities_and_edited_confirmation_does_not_reblock(self):
        initial = self._candidate_response()
        applied = self._apply(
            initial["candidates"],
            fingerprint=initial["source_fingerprint"],
        )
        self.assertEqual(applied.status_code, 200, applied.data)

        response = self.client.post(self.breakdown_url, {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        shot = self.project.episodes.get().shots.get()
        self.assertEqual(list(shot.characters.values_list("name", flat=True)), ["王强"])
        self.assertEqual(shot.scene.name, "大学图书馆")
        self.assertEqual(list(shot.props.values_list("name", flat=True)), ["红伞"])

    def test_body_only_change_keeps_all_candidates_confirmed(self):
        initial = self._candidate_response()
        applied = self._apply(
            initial["candidates"], fingerprint=initial["source_fingerprint"],
        )
        self.assertEqual(applied.status_code, 200, applied.data)

        self.project.master_script = self.project.master_script.replace(
            "这不是我的伞。", "这不是我的伞！",
        )
        self.project.save(update_fields=("master_script", "updated_at"))

        refreshed = self._candidate_response()
        self.assertNotEqual(
            refreshed["source_fingerprint"], initial["source_fingerprint"],
        )
        self.assertTrue(all(item["confirmed"] for item in refreshed["candidates"]))
        response = self.client.post(self.breakdown_url, {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_reviewed_rename_survives_later_body_change(self):
        one_character = {
            "characters": [bible_ir()["characters"][0]],
            "scenes": [],
            "props": [],
        }
        self.project.master_script = (
            f"{render_script_bible(one_character)}\n\n"
            "第1集：改名\n镜头1【时长=5秒】：王强进入。"
        )
        self.project.save(update_fields=("master_script", "updated_at"))
        initial = self._candidate_response()
        applied = self._apply(
            initial["candidates"],
            fingerprint=initial["source_fingerprint"],
            overrides_by_name={"王强": {"name": "王强（审核名）"}},
        )
        self.assertEqual(applied.status_code, 200, applied.data)

        self.project.master_script = self.project.master_script.replace(
            "王强进入。", "王强进入！",
        )
        self.project.save(update_fields=("master_script", "updated_at"))
        refreshed = self._candidate_response()
        self.assertTrue(refreshed["candidates"][0]["confirmed"])
        self.assertEqual(
            refreshed["candidates"][0]["matched_bible_id"],
            str(CharacterBible.objects.get().pk),
        )
        response = self.client.post(self.breakdown_url, {}, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_declaration_change_is_a_provenance_conflict(self):
        one_character = {
            "characters": [bible_ir()["characters"][0]],
            "scenes": [],
            "props": [],
        }
        self.project.master_script = (
            f"{render_script_bible(one_character)}\n\n"
            "第1集：声明\n镜头1【时长=5秒】：王强进入。"
        )
        self.project.save(update_fields=("master_script", "updated_at"))
        initial = self._candidate_response()
        applied = self._apply(
            initial["candidates"], fingerprint=initial["source_fingerprint"],
        )
        self.assertEqual(applied.status_code, 200, applied.data)

        changed_ir = one_character.copy()
        changed_ir["characters"] = [
            {**one_character["characters"][0], "description": "已经修改的声明"},
        ]
        self.project.master_script = (
            f"{render_script_bible(changed_ir)}\n\n"
            "第1集：声明\n镜头1【时长=5秒】：王强进入。"
        )
        self.project.save(update_fields=("master_script", "updated_at"))

        refreshed = self._candidate_response()
        candidate = refreshed["candidates"][0]
        self.assertFalse(candidate["confirmed"])
        self.assertEqual(
            candidate["conflict_code"], "script_bible_provenance_conflict",
        )
        blocked = self.client.post(self.breakdown_url, {}, format="json")
        self.assertEqual(blocked.status_code, 409, blocked.data)

    def test_reserved_provenance_cannot_be_forged_removed_replaced_or_synced(self):
        initial = self._candidate_response()
        character = next(
            item for item in initial["candidates"]
            if item["entity_type"] == "character"
        )
        applied = self._apply(
            [character], fingerprint=initial["source_fingerprint"],
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        saved = CharacterBible.objects.get()
        provenance = saved.metadata["script_bible_v1"]

        forged = self.client.post("/api/v1/characters/", {
            "project": str(self.project.pk),
            "slug": "forged",
            "name": "伪造人物",
            "stable_speaker_id": "S99",
            "metadata": {"script_bible_v1": provenance},
        }, format="json")
        self.assertEqual(forged.status_code, 400, forged.data)

        removed = self.client.patch(f"/api/v1/characters/{saved.pk}/", {
            "is_locked": False,
            "metadata": {},
        }, format="json")
        self.assertEqual(removed.status_code, 400, removed.data)

        replacement = {**provenance, "source_fingerprint": "0" * 64}
        replaced = self.client.patch(f"/api/v1/characters/{saved.pk}/", {
            "is_locked": False,
            "metadata": {"script_bible_v1": replacement},
        }, format="json")
        self.assertEqual(replaced.status_code, 400, replaced.data)

        type_replaced = self.client.patch(f"/api/v1/characters/{saved.pk}/", {
            "is_locked": False,
            "metadata": {
                "script_bible_v1": {**provenance, "schema_version": True},
            },
        }, format="json")
        self.assertEqual(type_replaced.status_code, 400, type_replaced.data)

        synced = self.client.post(
            f"/api/v1/projects/{self.project.pk}/sync-bibles/",
            {
                "characters": [{
                    "id": str(saved.pk),
                    "slug": saved.slug,
                    "name": saved.name,
                    "stable_speaker_id": saved.stable_speaker_id,
                    "metadata": {},
                }],
            },
            format="json",
        )
        self.assertEqual(synced.status_code, 400, synced.data)
        saved.refresh_from_db()
        self.assertEqual(saved.metadata["script_bible_v1"], provenance)
        self.assertEqual(CharacterBible.objects.count(), 1)

    def test_stale_full_bible_sync_cannot_delete_recent_candidate_apply(self):
        stale_updated_at = self.project.updated_at.isoformat()
        initial = self._candidate_response()
        character = next(
            item for item in initial["candidates"]
            if item["entity_type"] == "character"
        )

        applied = self._apply(
            [character],
            fingerprint=initial["source_fingerprint"],
        )

        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertEqual(CharacterBible.objects.count(), 1)

        stale_sync = self.client.post(
            f"/api/v1/projects/{self.project.pk}/sync-bibles/",
            {
                "expected_updated_at": stale_updated_at,
                "characters": [],
                "scenes": [],
                "props": [],
            },
            format="json",
        )

        self.assertEqual(stale_sync.status_code, 409, stale_sync.data)
        self.assertEqual(stale_sync.data["error"]["code"], "project_version_conflict")
        self.assertEqual(CharacterBible.objects.count(), 1)

    def test_invalid_reviewed_snapshot_hash_never_confirms(self):
        one_character = {
            "characters": [bible_ir()["characters"][0]],
            "scenes": [],
            "props": [],
        }
        self.project.master_script = (
            f"{render_script_bible(one_character)}\n\n"
            "第1集：伪造\n镜头1【时长=5秒】：王强进入。"
        )
        self.project.save(update_fields=("master_script", "updated_at"))
        parsed = parse_script_bible(self.project.master_script)
        candidate = parsed.candidates[0]
        CharacterBible.objects.create(
            project=self.project,
            slug="malicious-provenance",
            name="王强（数据库改名）",
            stable_speaker_id="S1",
            metadata={
                "script_bible_v1": {
                    "schema_version": 1,
                    "candidate_id": candidate["candidate_id"],
                    "declaration_fingerprint": candidate[
                        "declaration_fingerprint"
                    ],
                    "source_fingerprint": parsed.source_fingerprint,
                    "reviewed_snapshot": {
                        "name": candidate["name"],
                        "description": candidate["description"],
                        "visual_identity_en": candidate["visual_identity_en"],
                    },
                    "reviewed_content_sha256": "f" * 64,
                },
            },
        )

        response = self._candidate_response()
        projected = response["candidates"][0]
        self.assertFalse(projected["confirmed"])
        self.assertFalse(projected["existing"])
        self.assertEqual(
            projected["conflict_code"], "script_bible_provenance_conflict",
        )
        blocked = self.client.post(self.breakdown_url, {}, format="json")
        self.assertEqual(blocked.status_code, 409, blocked.data)

    def test_later_ordinary_bible_edits_preserve_valid_confirmation(self):
        one_character = {
            "characters": [bible_ir()["characters"][0]],
            "scenes": [],
            "props": [],
        }
        self.project.master_script = (
            f"{render_script_bible(one_character)}\n\n"
            "第1集：后续编辑\n镜头1【时长=5秒】：王强进入。"
        )
        self.project.save(update_fields=("master_script", "updated_at"))
        initial = self._candidate_response()
        applied = self._apply(
            initial["candidates"], fingerprint=initial["source_fingerprint"],
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        saved = CharacterBible.objects.get()

        unlocked = self.client.patch(
            f"/api/v1/characters/{saved.pk}/",
            {"is_locked": False},
            format="json",
        )
        self.assertEqual(unlocked.status_code, 200, unlocked.data)
        edited = self.client.patch(
            f"/api/v1/characters/{saved.pk}/",
            {
                "name": "王强（后续人工修订）",
                "description": "后续合法编辑，不改审核 provenance",
            },
            format="json",
        )
        self.assertEqual(edited.status_code, 200, edited.data)

        refreshed = self._candidate_response()
        self.assertTrue(refreshed["candidates"][0]["confirmed"])
        self.assertEqual(
            refreshed["candidates"][0]["matched_bible_id"], str(saved.pk),
        )
        allowed = self.client.post(self.breakdown_url, {}, format="json")
        self.assertEqual(allowed.status_code, 201, allowed.data)

    def test_block_without_episode_heading_uses_existing_fallback(self):
        self.project.master_script = master_script(heading=False)
        self.project.save(update_fields=("master_script", "updated_at"))
        initial = self._candidate_response()
        applied = self._apply(
            initial["candidates"], fingerprint=initial["source_fingerprint"],
        )
        self.assertEqual(applied.status_code, 200, applied.data)

        response = self.client.post(self.breakdown_url, {
            "episode_count_hint": 1,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.project.episodes.count(), 1)

    def test_longer_overlapping_name_is_the_only_generic_binding(self):
        self.project.master_script = (
            "第1集：重名\n"
            "镜头1【时长=5秒】：走廊\n"
            "[0-5秒] 动作：王强独自穿过走廊。"
        )
        self.project.save(update_fields=("master_script", "updated_at"))
        short = CharacterBible.objects.create(
            project=self.project, slug="wang", name="王", stable_speaker_id="S1",
        )
        long = CharacterBible.objects.create(
            project=self.project, slug="wang-qiang", name="王强", stable_speaker_id="S2",
        )

        response = self.client.post(self.breakdown_url, {}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        bound = set(self.project.episodes.get().shots.get().characters.values_list("id", flat=True))
        self.assertEqual(bound, {long.pk})
        self.assertNotIn(short.pk, bound)

    def test_provenance_hash_fields_reject_integer_lookalikes(self):
        snapshot = {
            "name": "Reviewed Name",
            "description": "Reviewed description",
            "visual_identity_en": "reviewed visual identity",
        }
        provenance = {
            "schema_version": 1,
            "candidate_id": "a" * 64,
            "declaration_fingerprint": "b" * 64,
            "source_fingerprint": "c" * 64,
            "reviewed_snapshot": snapshot,
            "reviewed_content_sha256": script_bible_reviewed_content_sha256(
                snapshot,
            ),
        }
        self.assertIsNotNone(_valid_reviewed_provenance_sha256(provenance))
        integer_lookalike = int("1" * 64)
        for field in (
            "candidate_id",
            "declaration_fingerprint",
            "source_fingerprint",
            "reviewed_content_sha256",
        ):
            with self.subTest(field=field):
                self.assertIsNone(_valid_reviewed_provenance_sha256({
                    **provenance,
                    field: integer_lookalike,
                }))

    def test_reviewed_names_bind_all_three_original_declarations(self):
        initial = self._candidate_response()
        reviewed_names = {
            "character": "Reviewed Character",
            "scene": "Reviewed Scene",
            "prop": "Reviewed Prop",
        }
        applied = self._apply(
            initial["candidates"],
            fingerprint=initial["source_fingerprint"],
            overrides_by_name={
                candidate["name"]: {
                    "name": reviewed_names[candidate["entity_type"]],
                }
                for candidate in initial["candidates"]
            },
        )
        self.assertEqual(applied.status_code, 200, applied.data)

        response = self.client.post(self.breakdown_url, {}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        character = CharacterBible.objects.get()
        scene = SceneBible.objects.get()
        prop = PropBible.objects.get()
        shot = self.project.episodes.get().shots.get()
        self.assertEqual(set(shot.characters.values_list("pk", flat=True)), {
            character.pk,
        })
        self.assertEqual(shot.scene_id, scene.pk)
        self.assertEqual(set(shot.props.values_list("pk", flat=True)), {
            prop.pk,
        })
        self.assertEqual(shot.dialogue[0]["speaker_id"], character.stable_speaker_id)

    def test_reviewed_aliases_keep_longest_non_overlapping_match(self):
        source_ir = {
            "characters": [
                {
                    "name": "\u738b",
                    "description": "short name",
                    "visual_identity_en": "short-name character",
                },
                {
                    "name": "\u738b\u5f3a",
                    "description": "long name",
                    "visual_identity_en": "long-name character",
                },
            ],
            "scenes": [],
            "props": [],
        }
        body = (
            "\u7b2c1\u96c6\uff1aAlias\n"
            "\u955c\u59341\u3010\u65f6\u957f 5\u79d2\u3011\uff1a\u8d70\u5eca\n"
            "[0-5\u79d2] \u52a8\u4f5c\uff1a\u738b\u5f3a\u72ec\u81ea\u7a7f\u8fc7\u8d70\u5eca\u3002"
        )
        self.project.master_script = f"{render_script_bible(source_ir)}\n\n{body}"
        self.project.save(update_fields=("master_script", "updated_at"))
        initial = self._candidate_response()
        applied = self._apply(
            initial["candidates"],
            fingerprint=initial["source_fingerprint"],
            overrides_by_name={
                "\u738b": {"name": "Reviewed Short"},
                "\u738b\u5f3a": {"name": "Reviewed Long"},
            },
        )
        self.assertEqual(applied.status_code, 200, applied.data)

        response = self.client.post(self.breakdown_url, {}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        short = CharacterBible.objects.get(name="Reviewed Short")
        long = CharacterBible.objects.get(name="Reviewed Long")
        bound = set(
            self.project.episodes.get().shots.get().characters.values_list(
                "pk", flat=True,
            ),
        )
        self.assertEqual(bound, {long.pk})
        self.assertNotIn(short.pk, bound)

    def test_stale_crud_serializer_cannot_overwrite_concurrent_apply_provenance(self):
        saved = CharacterBible.objects.create(
            project=self.project,
            slug="stale-crud",
            name="Stale CRUD",
            stable_speaker_id="S99",
            metadata={},
            is_locked=False,
        )
        parsed = parse_script_bible(self.project.master_script)
        candidate = parsed.candidates[0]
        snapshot = {
            "name": candidate["name"],
            "description": candidate["description"],
            "visual_identity_en": candidate["visual_identity_en"],
        }
        provenance = {
            "schema_version": 1,
            "candidate_id": candidate["candidate_id"],
            "declaration_fingerprint": candidate["declaration_fingerprint"],
            "source_fingerprint": parsed.source_fingerprint,
            "reviewed_snapshot": snapshot,
            "reviewed_content_sha256": script_bible_reviewed_content_sha256(
                snapshot,
            ),
        }
        request = APIRequestFactory().patch(
            f"/api/v1/characters/{saved.pk}/",
            {"metadata": {"ordinary_note": "stale request"}},
            format="json",
        )
        request.user = self.user
        serializer = CharacterBibleSerializer(
            saved,
            data={"metadata": {"ordinary_note": "stale request"}},
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        CharacterBible.objects.filter(pk=saved.pk).update(
            metadata={"script_bible_v1": provenance},
        )
        view = CharacterBibleViewSet()
        view.request = request

        with self.assertRaises(ValidationError):
            view.perform_update(serializer)

        saved.refresh_from_db()
        self.assertEqual(saved.metadata, {"script_bible_v1": provenance})

    def test_stable_speaker_id_wins_over_confirmed_name_alias_collision(self):
        source_ir = {
            "characters": [{
                "name": "S1",
                "description": "declaration that resembles a speaker id",
                "visual_identity_en": "student wearing a grey coat",
            }],
            "scenes": [],
            "props": [],
        }
        self.project.master_script = (
            f"{render_script_bible(source_ir)}\n\n"
            "\u7b2c1\u96c6\uff1aCollision\n"
            "\u955c\u59341\u3010\u65f6\u957f 5\u79d2\u3011\uff1a\u8d70\u5eca\n"
            "[0-5\u79d2] S1\uff1aExplicit speaker id wins."
        )
        self.project.save(update_fields=("master_script", "updated_at"))
        actual_s1 = CharacterBible.objects.create(
            project=self.project,
            slug="actual-s1",
            name="Actual S1 Speaker",
            stable_speaker_id="S1",
        )
        initial = self._candidate_response()
        applied = self._apply(
            initial["candidates"],
            fingerprint=initial["source_fingerprint"],
            overrides_by_name={"S1": {"name": "Reviewed Alias Character"}},
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertEqual(
            CharacterBible.objects.get(name="Reviewed Alias Character").stable_speaker_id,
            "S2",
        )

        response = self.client.post(self.breakdown_url, {}, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        shot = self.project.episodes.get().shots.get()
        self.assertEqual(shot.dialogue[0]["speaker_id"], "S1")
        self.assertEqual(
            set(shot.characters.values_list("pk", flat=True)),
            {actual_s1.pk},
        )
