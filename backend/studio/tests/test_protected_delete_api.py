from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import ProductionRun, ReleaseBundle
from studio.tests.factories import build_story


class ProtectedDeleteAPITests(TestCase):
    def test_protected_release_evidence_returns_conflict_instead_of_server_error(self):
        user, project, *_ = build_story("protected-delete")
        run = ProductionRun.objects.create(owner=user, project=project, plan_source="manual",
            idempotency_key="protected-release-run", request_sha256="a" * 64)
        ReleaseBundle.objects.create(
            production_run=run,
            owner=user,
            project=project,
            created_by=user,
            version=1,
            title="不可删除的发行证据",
            selection={"shots": [], "episodes": [], "series_artifact_id": None},
        )
        client = APIClient()
        client.force_authenticate(user)

        response = client.delete(f"/api/v1/projects/{project.id}/")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "protected_resource")
        self.assertTrue(type(project).objects.filter(pk=project.pk).exists())
