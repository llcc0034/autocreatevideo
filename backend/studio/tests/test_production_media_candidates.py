"""Actual archived media candidate facts before explicit adoption or assembly."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import ArtifactSelectionDecision
from studio.services.execution_production_candidates import read_production_media_candidate
from studio.services.json_contracts import assign_json_contract
from studio.tests import test_execution_production_workflow_artifacts as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ProductionMediaCandidateTests(TestCase):
    resolve = fixtures.ProductionWorkflowArtifactTests.resolve
    mark_submitted = fixtures.ProductionWorkflowArtifactTests.mark_submitted
    archive = fixtures.ProductionWorkflowArtifactTests.archive

    def setUp(self):
        fixtures.ProductionWorkflowArtifactTests.setUp(self)
        self.artifact, = self.archive()

    def test_candidate_read_uses_archived_bytes_and_exact_unit_without_adoption_or_recompilation(self):
        with patch("studio.services.execution_preparation_requests.compile_prepared_execution_transport", side_effect=AssertionError("No compile on read")):
            result = read_production_media_candidate(self.artifact, max_media_bytes=65536)
        self.assertEqual(result["preparation_id"], str(self.preparation.pk))
        self.assertEqual(result["production_unit"]["scope"], {"kind": "project", "id": str(self.project.pk)})
        self.assertEqual(result["media_facts"]["sha256"], self.artifact.sha256)
        self.assertEqual(result["media_facts"]["observed_media"]["width"], 512)
        self.assertTrue(result["path"].is_file())
        self.assertEqual(self.artifact.status, "available")
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_resealed_wrong_identity_failed_attempt_and_changed_bytes_are_not_candidates(self):
        original = deepcopy(self.artifact.manifest_metadata)
        for key, value in (("unit_key", "other"), ("production_unit", {}), ("source", "restricted_async_rest"),
                ("preparation_id", str(self.run.pk))):
            with self.subTest(key=key):
                assign_json_contract(self.artifact, "manifest_metadata", {**original, key: value})
                with self.assertRaises(ValidationError):
                    read_production_media_candidate(self.artifact, max_media_bytes=65536)
        self.artifact.refresh_from_db()
        self.attempt.refresh_from_db()
        self.attempt.status = "failed"
        self.attempt.save(update_fields=("status",))
        with self.assertRaises(ValidationError):
            read_production_media_candidate(self.artifact, max_media_bytes=65536)
        self.attempt.status = "succeeded"
        self.attempt.save(update_fields=("status",))
        result = read_production_media_candidate(self.artifact, max_media_bytes=65536)
        result["path"].write_bytes(b"changed archive")
        with self.assertRaises(ValidationError):
            read_production_media_candidate(self.artifact, max_media_bytes=65536)
