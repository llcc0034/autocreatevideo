from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection, transaction, IntegrityError
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import Shot, ShotFieldRevision, ShotScriptSource
from studio.services.production_storyboard_content import storyboard_content
from studio.services.shot_field_revisions import _validate_overrides, shot_field_revision_references
from studio.tests import test_storyboard_declared_writer as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ShotFieldRevisionTests(TestCase):
    create = fixtures.BoundStoryboardFieldTests.create
    post = fixtures.BoundStoryboardFieldTests.post
    read = fixtures.BoundStoryboardFieldTests.read
    adopt_source = fixtures.BoundStoryboardFieldTests.adopt_source
    approval = fixtures.BoundStoryboardFieldTests.approval
    setUp = fixtures.BoundStoryboardFieldTests.setUp

    def revision_url(self):
        return f"/api/v1/shots/{self.shot_id}/field-revisions/"

    def detail(self):
        response = self.client.get(self.revision_url())
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def field_payload(self, value="  修订原文\n", detail=None):
        detail = detail or self.detail()
        field = next(row["original"] for row in detail["fields"] if row["original"].get("value") == "紧张")
        return {"request_id": str(uuid4()), "expected_revision_id": detail["revision"]["id"] if detail["revision"] else None,
            "source_sha256": detail["source_sha256"], "node_key": field["node_key"],
            "value_path": field["value_path"], "operation": "set", "value": value}

    def write(self, payload):
        return self.client.post(self.revision_url(), payload, format="json")

    def test_raw_manual_value_exact_original_and_other_business_facts_preserved(self):
        original = self.detail()
        before = fixtures.DeclaredStoryboardWriterTests.other_facts(self)
        shots = list(Shot.objects.values())
        source = self.client.get(f"/api/v1/shots/{self.shot_id}/source-fields/").data
        response = self.write(self.field_payload())
        self.assertEqual(response.status_code, 201, response.data)
        result = response.data["detail"]
        revised = [row for row in result["fields"] if row["overridden"]]
        self.assertEqual(len(revised), 1)
        self.assertEqual(revised[0]["value"], "  修订原文\n")
        self.assertEqual(revised[0]["original"]["value"], "紧张")
        self.assertEqual([row["original"] for row in result["fields"]], [row["original"] for row in original["fields"]])
        self.assertEqual(result["revision"]["created_by"], self.project.owner_id)
        self.assertEqual(list(Shot.objects.values()), shots)
        self.assertEqual(fixtures.DeclaredStoryboardWriterTests.other_facts(self), before)
        self.assertEqual(self.client.get(f"/api/v1/shots/{self.shot_id}/source-fields/").data, source)

    def test_idempotent_replay_old_revision_conflict_and_explicit_reset(self):
        payload = self.field_payload()
        first = self.write(payload)
        self.assertEqual(first.status_code, 201, first.data)
        reset = self.field_payload(detail=first.data["detail"])
        reset.update(operation="reset"); reset.pop("value")
        second = self.write(reset)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(second.data["detail"]["revision"]["revision"], 2)
        self.assertFalse(any(row["overridden"] for row in second.data["detail"]["fields"]))
        with CaptureQueriesContext(connection) as queries:
            replay = self.write(payload)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["detail"], first.data["detail"])
        self.assertTrue(replay.data["replayed"])
        self.assertTrue(fixtures.no_dml(queries))
        self.assertEqual(self.detail()["revision"]["revision"], 2)
        self.assertEqual(self.client.get(self.revision_url(), {"revision_id": first.data["detail"]["revision"]["id"]}).data,
            first.data["detail"])
        self.assertEqual(self.write({**payload, "value": "不同"}).status_code, 409)
        self.assertEqual(self.write({**payload, "request_id": str(uuid4())}).status_code, 409)
        self.assertEqual(ShotFieldRevision.objects.count(), 2)

    def test_type_unknown_paths_stale_basis_invalid_request_and_limits_are_atomic(self):
        payload = self.field_payload()
        for changes, status in (({"value": False}, 400), ({"value": None}, 400), ({"value_path": [True]}, 400),
                ({"node_key": "other"}, 400), ({"source_sha256": "0" * 64}, 409),
                ({"unknown": "silently ignored?"}, 400), ({"operation": "reset"}, 400),
                ({"value": "界" * 65536}, 400)):
            response = self.write({**payload, **changes})
            self.assertEqual(response.status_code, status, response.data)
            self.assertFalse(ShotFieldRevision.objects.exists())
        no_value = deepcopy(payload); no_value.pop("value")
        self.assertEqual(self.write(no_value).status_code, 400)

    def test_scope_missing_and_tampered_source_do_not_fall_back(self):
        payload = self.field_payload()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.revision_url()).status_code, 404)
        self.assertEqual(self.write(payload).status_code, 404)
        self.client.force_authenticate(self.project.owner)
        self.assertEqual(self.client.get(self.revision_url(), {"revision_id": str(uuid4())}).status_code, 409)
        self.assertEqual(self.client.get(self.revision_url(), {"revision_id": "bad"}).status_code, 400)
        ShotScriptSource.objects.filter(pk=self.shot_id).update(document_sha256="0" * 64)
        self.assertEqual(self.write(payload).status_code, 409)
        self.assertEqual(self.client.get(self.revision_url()).status_code, 409)
        self.assertFalse(ShotFieldRevision.objects.exists())

    def test_revision_immutable_and_database_keys(self):
        response = self.write(self.field_payload()); self.assertEqual(response.status_code, 201, response.data)
        row = ShotFieldRevision.objects.get()
        for action in (lambda: row.save(), lambda: row.delete(),
                lambda: ShotFieldRevision.objects.update(overrides=[]), lambda: ShotFieldRevision.objects.all().delete()):
            with self.assertRaises(ValidationError): action()
        copy = {field.attname: getattr(row, field.attname) for field in row._meta.fields
            if field.attname not in {"id", "created_at", "updated_at"}}
        with self.assertRaises(IntegrityError), transaction.atomic():
            ShotFieldRevision.objects.create(**copy)
        copy["revision"] = 0; copy["request_id"] = uuid4()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ShotFieldRevision.objects.create(**copy)

    def test_review_tracks_revision_but_does_not_load_values_and_original_read_is_pure(self):
        episodes = list(self.project.episodes.all())
        before = storyboard_content(project_id=self.project.pk, episodes=episodes)
        response = self.write(self.field_payload()); self.assertEqual(response.status_code, 201, response.data)
        after = storyboard_content(project_id=self.project.pk, episodes=episodes)
        self.assertNotEqual(before, after)
        with CaptureQueriesContext(connection) as queries:
            refs = shot_field_revision_references([self.shot_id])
        self.assertEqual(len(queries), 1)
        self.assertEqual(refs[self.shot_id]["id"], response.data["detail"]["revision"]["id"])
        self.assertNotIn('"overrides"', queries[0]["sql"])
        self.assertIn("INDEX", ShotFieldRevision.objects.filter(shot_id=self.shot_id).explain())
        with patch("studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError()), \
                CaptureQueriesContext(connection) as queries:
            self.detail()
        self.assertTrue(fixtures.no_dml(queries))
        self.assertFalse(any('"studio_fielddefinition' in row["sql"] for row in queries))

    def test_read_validation_failure_rolls_back_new_revision(self):
        payload = self.field_payload()
        with patch("studio.services.shot_field_revisions._detail", side_effect=ValidationError("isolated failure")):
            self.assertEqual(self.write(payload).status_code, 400)
        self.assertFalse(ShotFieldRevision.objects.exists())

    def test_corrupt_stored_body_and_cross_actor_replay_are_conflicts(self):
        payload = self.field_payload()
        response = self.write(payload)
        self.assertEqual(response.status_code, 201, response.data)
        self.other.is_staff = True; self.other.save(update_fields=("is_staff",))
        self.client.force_authenticate(self.other)
        self.assertEqual(self.write(payload).status_code, 409)
        # Deliberate isolated database corruption: bypass append-only manager.
        with connection.cursor() as cursor:
            cursor.execute('UPDATE studio_shotfieldrevision SET overrides = %s', ['[]'])
        self.assertEqual(self.client.get(self.revision_url()).status_code, 409)
        self.assertEqual(self.write(payload).status_code, 409)


class FrozenFieldRevisionTypeTests(SimpleTestCase):
    def test_all_json_types_and_repeated_node_paths_use_original_contract(self):
        from studio.tests.output_structure_fixtures import frozen_response_structure
        from studio.services.script_storyboard_fields import storyboard_source_fields
        from studio.tests.test_storyboard_source_fields import document
        from studio.tests.script_semantics_fixtures import each, literal, obj, path

        raw = {"frames": [{"number": 3, "zero": 0, "flag": False, "text": "", "nullable": "text",
            "items": [], "object": {}, "optional": "sample", '自定义.["说明"]': [{"line": " first "}, {"line": "second"}]}]}
        structure = frozen_response_structure(raw)
        for node in structure["nodes"]:
            if node["property_name"] == "nullable": node["allow_null"] = True
            if node["property_name"] == "optional": node["required"] = False
        del raw["frames"][0]["optional"]
        mapping = {"schema_version": 1, "node": obj(entities=literal([]), episodes={"kind": "array", "items": [
            obj(episode=literal(8), shots=each(path("frames", scope="root"), obj(shot=path("number"))))]})}
        fields = storyboard_source_fields(document(raw, structure, mapping), max_bytes=65536)[8, 3]
        original = {"fields": fields}
        before = deepcopy(original)
        overrides = []
        for name, value in (("zero", 0), ("flag", False), ("text", "  修订\n"), ("nullable", None),
                ("items", []), ("object", {}), ("optional", "人工填补")):
            field = next(row for row in fields if row["value_path"][-1] == name)
            overrides.append({"node_key": field["node_key"], "value_path": field["value_path"], "value": value})
        _validate_overrides(overrides, original, structure)
        for changed in ({**overrides[0], "value": False}, {**overrides[2], "value": None},
                {**overrides[0], "value_path": ["frames", 5, "zero"]}):
            with self.assertRaises(ValidationError): _validate_overrides([changed], original, structure)
        lines = [row for row in fields if row["value_path"][-1] == "line"]
        self.assertEqual(lines[0]["node_key"], lines[1]["node_key"])
        revisions = [{"node_key": row["node_key"], "value_path": row["value_path"], "value": str(index)} for index, row in enumerate(lines)]
        _validate_overrides(revisions, original, structure)
        with self.assertRaises(ValidationError): _validate_overrides([revisions[0], revisions[0]], original, structure)
        self.assertEqual(original, before)
