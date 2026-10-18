from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class ExecutionPromptSchemaTests(SimpleTestCase):
    def test_complete_prompt_is_revision_guarded_read_only_with_explicit_modes(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        path = schema["paths"]["/api/v1/execution-preparations/{id}/prompt-display/"]
        self.assertEqual(set(path), {"get"})
        self.assertIn("409", path["get"]["responses"])
        self.assertTrue(next(row for row in path["get"]["parameters"] if row["name"] == "expected_preparation_revision")["required"])
        fields = schema["components"]["schemas"]["ExecutionPromptDisplay"]["properties"]
        self.assertEqual(set(fields), {"preparation_id", "preparation_revision", "mode", "request_sha256", "frozen_at", "channels", "field_basis", "sources", "issues", "length_diagnostics", "profile_basis"})
