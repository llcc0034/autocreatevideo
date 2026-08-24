from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import ShotDetailDraft
from studio.services.direction_semantics import direction_semantic_mismatches
from studio.tests.factories import build_story


RUNTIME_SHOT_DETAIL_POLICY = {
    "recommended_zh_chars": 500,
    "recommended_en_chars": 750,
    "transport_zh_max_chars": 3000,
    "transport_en_max_chars": 4500,
}


def _load_runtime(module_name: str):
    runtime_path = Path(__file__).resolve().parents[3] / "scripts" / "qwen_translate_once.py"
    spec = importlib.util.spec_from_file_location(module_name, runtime_path)
    assert spec and spec.loader
    runtime = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runtime
    spec.loader.exec_module(runtime)
    return runtime


class ShotDetailDraftReadOnlyApiTests(TestCase):
    def setUp(self):
        (
            self.user,
            self.project,
            _character,
            _scene,
            self.episode,
            self.shot,
            _next_shot,
        ) = build_story()
        self.draft = ShotDetailDraft.objects.create(
            project=self.project,
            episode=self.episode,
            shot=self.shot,
            created_by=self.user,
            context_hash="a" * 64,
            context_snapshot={"schema_version": 1},
            draft_data={
                "schema_version": 1,
                "detail_zh": "镜头缓慢推近人物。",
                "detail_en": "The camera slowly pushes toward the subject.",
            },
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_owner_can_filter_historical_drafts_without_mutating_them(self):
        before = self.draft.updated_at
        response = self.client.get(
            f"/api/v1/projects/{self.project.pk}/shot-detail-drafts/",
            {"shot": str(self.shot.pk)},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data], [str(self.draft.pk)])
        self.assertTrue({
            "context_current", "can_edit", "can_regenerate", "can_confirm", "can_apply",
        }.isdisjoint(response.data[0]))
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.updated_at, before)

    def test_read_only_history_is_tenant_scoped(self):
        other = get_user_model().objects.create_user(
            username="shot-detail-outsider", password="StrongPass123!",
        )
        self.client.force_authenticate(other)

        response = self.client.get(
            f"/api/v1/projects/{self.project.pk}/shot-detail-drafts/",
        )

        self.assertEqual(response.status_code, 404)


class ShotDetailRuntimeValidationTests(TestCase):
    def test_server_direction_semantics_excludes_ordinary_left_right_words(self):
        self.assertEqual(
            direction_semantic_mismatches("她离开房间。", "She left the room."), [],
        )
        self.assertEqual(
            direction_semantic_mismatches("现在就行动。", "Act right now."), [],
        )
        self.assertEqual(
            direction_semantic_mismatches("两侧都有守卫。", "Guards stand on either side."), [],
        )
        self.assertEqual(
            direction_semantic_mismatches("她靠近门口。", "She approaches from either side."), [],
        )

    def test_runtime_keeps_parseable_language_issues_for_human_review(self):
        runtime = _load_runtime("qwen_detail_runtime_for_test")
        request = SimpleNamespace(glossary={}, policy=RUNTIME_SHOT_DETAIL_POLICY)
        for invalid_english in (
            "日本語 director prose", "한국어 director prose", "Русский prose",
            "Greek α director prose", "Arabic العربية prose",
        ):
            issues = []
            data = runtime._validate_shot_high_detail({
                "schema_version": 1,
                "detail_zh": "中文细节说明。",
                "detail_en": invalid_english,
            }, request, review_issues=issues)
            self.assertEqual(data["detail_en"], invalid_english)
            self.assertTrue(issues)
        with self.assertRaises(RuntimeError):
            runtime._validate_shot_high_detail({
                "schema_version": 1,
                "detail_zh": "",
                "detail_en": "",
            }, request)

    def test_runtime_materialises_omitted_constant_schema_version_for_json_object_mode(self):
        runtime = _load_runtime("qwen_detail_json_object_for_test")
        candidate = runtime._validate_shot_high_detail({
            "detail_zh": "镜头缓慢向右移动，人物停在门口。",
            "detail_en": "The camera slowly moves right as the subject stops at the doorway.",
        }, SimpleNamespace(
            glossary={}, policy=RUNTIME_SHOT_DETAIL_POLICY,
        ), review_issues=[])
        self.assertEqual(candidate["schema_version"], 1)
        self.assertIn("detail_en", candidate)

    def test_runtime_normalises_one_known_wrapper_aliases_and_extra_keys_for_review(self):
        runtime = _load_runtime("qwen_detail_shape_for_test")
        issues = []
        candidate = runtime._validate_shot_high_detail({
            "schema_version": 1,
            "result": {
                "chinese_detail": "镜头缓慢向右横移，人物停在门口。",
                "english": "The camera slowly tracks right as the subject stops at the doorway.",
                "explanation": "ignored operator note",
            },
        }, SimpleNamespace(
            glossary={}, policy=RUNTIME_SHOT_DETAIL_POLICY,
        ), review_issues=issues)
        self.assertEqual(candidate["schema_version"], 1)
        self.assertEqual(candidate["detail_zh"], "镜头缓慢向右横移，人物停在门口。")
        self.assertTrue(any(
            issue["code"] == "model_output_shape_normalized" for issue in issues
        ))

    def test_runtime_keeps_single_known_language_field_as_editable_review_candidate(self):
        runtime = _load_runtime("qwen_detail_partial_for_test")
        issues = []
        candidate = runtime._validate_shot_high_detail({
            "detail_cn": "镜头缓慢向右横移，人物停在门口。",
        }, SimpleNamespace(
            glossary={}, policy=RUNTIME_SHOT_DETAIL_POLICY,
        ), review_issues=issues)
        self.assertEqual(candidate["detail_en"], "")
        self.assertTrue(any(issue["code"] == "detail_en_missing" for issue in issues))
