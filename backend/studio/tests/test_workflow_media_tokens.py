from django.test import SimpleTestCase

from studio.services.workflow_media_tokens import workflow_media_use_token, is_workflow_media_use_token, MEDIA_USE_TOKEN_PREFIX
from studio.services.workflow_semantics import _validate_value, WorkflowSemanticError


class WorkflowMediaTokenTests(SimpleTestCase):
    def test_identity_is_stable_for_one_use_but_changes_with_use_or_source(self):
        first = workflow_media_use_token(item_key="first-use", source_sha256="a" * 64)
        self.assertEqual(first, workflow_media_use_token(item_key="first-use", source_sha256="a" * 64))
        self.assertNotEqual(first, workflow_media_use_token(item_key="second-use", source_sha256="a" * 64))
        self.assertNotEqual(first, workflow_media_use_token(item_key="first-use", source_sha256="b" * 64))
        self.assertTrue(is_workflow_media_use_token(first))
        self.assertNotIn("first-use", first)

    def test_invalid_or_unbounded_identity_cannot_create_a_placeholder(self):
        for item, sha in (("", "a" * 64), ("x" * 192, "a" * 64), (True, "a" * 64), ("use", ""), ("use", "not-a-sha")):
            with self.subTest(item=item, sha=sha), self.assertRaises(ValueError):
                workflow_media_use_token(item_key=item, source_sha256=sha)

    def test_shared_semantic_validator_accepts_only_complete_use_placeholders(self):
        token = workflow_media_use_token(item_key="actual-use", source_sha256="a" * 64)
        self.assertEqual(_validate_value("asset.image.source", token, {}, "asset_image"), token)
        for value in (MEDIA_USE_TOKEN_PREFIX, MEDIA_USE_TOKEN_PREFIX + "invalid", "unverified-local-file.png"):
            with self.subTest(value=value), self.assertRaises(WorkflowSemanticError):
                _validate_value("asset.image.source", value, {}, "asset_image")
