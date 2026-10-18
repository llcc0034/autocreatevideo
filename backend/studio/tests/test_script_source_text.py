"""Original script HTTP/source boundaries preserve actual text and byte identity."""
from hashlib import sha256
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ScriptRevision, ScriptSource, SeriesProject
from studio.services.script_sources import (
    ScriptSourceValidationError, _validate_text, create_pasted_script_source, create_script_revision,
    normalize_script_asset_upload,
)
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class ScriptSourceTextTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="exact-source-owner")
        self.other = get_user_model().objects.create_user(username="exact-source-other")
        self.project = SeriesProject.objects.create(owner=self.owner, title="Exact original text", **PROJECT_CONFIG)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.text = ' \r\n{\r\n "对白": "等一下！",\r "flag": false, "zero": 0\n}\t '

    def create(self, **changes):
        return self.client.post("/api/v1/script-sources/", {"project_id": str(self.project.pk),
            "content": self.text, **changes}, format="json")

    def assert_text(self, revision, content):
        encoded = content.encode("utf-8")
        self.assertEqual(revision["content"], content)
        self.assertEqual(revision["content_size_bytes"], len(encoded))
        self.assertEqual(revision["content_sha256"], sha256(encoded).hexdigest())
        self.assertEqual(revision["status"], "preview")

    def test_existing_paste_api_preserves_leading_trailing_space_and_all_newlines(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.data)
        self.assert_text(response.data["revision"], self.text)
        revision = self.client.get(f'/api/v1/script-revisions/{response.data["revision"]["id"]}/')
        self.assertEqual(revision.status_code, 200)
        self.assert_text(revision.data, self.text)
        self.assertEqual(response.data["source"]["file_sha256"], sha256(self.text.encode()).hexdigest())
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, "")

    def test_revision_api_actually_uses_declared_serializer_and_preserves_new_revision(self):
        response = self.create()
        original_id = response.data["revision"]["id"]
        source_id = response.data["source"]["id"]
        text = " \r\n原文第二版\r\n "
        edited = self.client.post("/api/v1/script-revisions/", {"source_id": source_id, "content": text}, format="json")
        self.assertEqual(edited.status_code, 201, edited.data)
        self.assert_text(edited.data, text)
        self.assertNotEqual(edited.data["id"], original_id)
        self.assertEqual(ScriptRevision.objects.get(pk=original_id).content, self.text)
        self.assertEqual(edited.data["revision"], 2)

    def test_http_rejects_non_text_without_stringifying_values_or_writing_rows(self):
        for content in (False, 0, 123, ["text"], {"text": "not source"}, None):
            with self.subTest(content=content):
                self.assertEqual(self.create(content=content).status_code, 400)
        self.assertEqual(ScriptSource.objects.count(), 0)
        source = self.create().data["source"]["id"]
        for content in (False, 0, [], {}, None):
            response = self.client.post("/api/v1/script-revisions/", {"source_id": source, "content": content}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ScriptRevision.objects.count(), 1)
        self.assertEqual(self.client.post("/api/v1/script-revisions/", {"source_id": "bad-id", "content": "text"}, format="json").status_code, 400)

    def test_blank_and_nul_stay_explicit_failures(self):
        for content in ("", " \r\n\t ", "a\x00b"):
            response = self.create(content=content)
            self.assertIn(response.status_code, (400, 422), response.data)
        self.assertFalse(ScriptSource.objects.exists())
        source = self.create().data["source"]["id"]
        for content in ("", " \r\n ", "a\x00b"):
            response = self.client.post("/api/v1/script-revisions/", {"source_id": source, "content": content}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ScriptRevision.objects.count(), 1)

    def test_byte_limit_counts_actual_crlf_bytes_not_a_shorter_replacement(self):
        text = "x\r\ny"
        self.assertEqual(_validate_text(text, max_bytes=4), (text, 4, sha256(text.encode()).hexdigest()))
        with self.assertRaises(ScriptSourceValidationError) as error:
            _validate_text(text, max_bytes=3)
        self.assertEqual(error.exception.code, "script_too_large")
        with patch("studio.services.script_sources._upload_limit", return_value=3):
            response = self.create(content=text)
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ScriptSource.objects.exists())

    def test_service_rejects_invalid_unicode_and_non_text_before_creating_source(self):
        for content in (None, 42, False, {}, "unpaired\ud800"):
            with self.subTest(content=repr(content)), self.assertRaises(ScriptSourceValidationError):
                create_pasted_script_source(project=self.project, user=self.owner, content=content)
        self.assertFalse(ScriptSource.objects.exists())

    def test_shared_service_keeps_raw_model_response_and_revision_edits(self):
        for source_type in ("paste", "ai_generated"):
            source, revision = create_pasted_script_source(project=self.project, user=self.owner,
                content=self.text, source_type=source_type)
            self.assertEqual(revision.content, self.text)
            self.assertEqual(source.file_sha256, revision.content_sha256)
            if source_type == "paste":
                edited = create_script_revision(source=source, user=self.owner, content="\r\n新稿\r ")
                self.assertEqual(edited.content, "\r\n新稿\r ")
            else:
                self.assertIsNone(revision.origin_generation_attempt_id)

    def test_upload_preserves_extracted_newlines_and_separate_original_file(self):
        with TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            response = self.client.post("/api/v1/script-sources/", {"project_id": str(self.project.pk),
                "file": SimpleUploadedFile("raw.txt", self.text.encode(), content_type="text/plain")}, format="multipart")
            self.assertEqual(response.status_code, 201, response.data)
            self.assert_text(response.data["revision"], self.text)
            source = ScriptSource.objects.get(pk=response.data["source"]["id"])
            with source.file.open("rb") as handle:
                self.assertEqual(handle.read(), self.text.encode())

    def test_library_ingestion_keeps_its_explicit_normalization_contract(self):
        data, metadata = normalize_script_asset_upload(SimpleUploadedFile("raw.txt", self.text.encode(), content_type="text/plain"))
        self.assertEqual(data, self.text.replace("\r\n", "\n").replace("\r", "\n").encode())
        self.assertEqual(metadata["script_normalization"], "utf8_markdown_v1")
        self.assertEqual(metadata["original_sha256"], sha256(self.text.encode()).hexdigest())
        self.assertEqual(metadata["text_sha256"], sha256(data).hexdigest())

    def test_visibility_and_no_implicit_confirmation_are_unchanged(self):
        created = self.create()
        source = created.data["source"]["id"]
        revision = created.data["revision"]["id"]
        self.client.force_authenticate(self.other)
        self.assertEqual(self.create().status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/script-revisions/{revision}/").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/script-revisions/", {"source_id": source, "content": "不允许"}, format="json").status_code, 404)
        self.assertEqual(ScriptRevision.objects.count(), 1)
        self.assertIsNone(ScriptRevision.objects.get(pk=revision).confirmed_at)
