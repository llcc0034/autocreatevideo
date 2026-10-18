"""Exact confirmed content inputs; no invented ScriptRevision or project."""
from copy import deepcopy
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext

from studio.models import ScriptRevision, SeriesProject, Style
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_native_text import native_text_source_reference, build_native_text_plan
from studio.services.json_contracts import json_contract
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision, create_script_revision
from studio.services.script_semantic_requests import compile_script_semantic_plan, verify_script_semantic_request
from studio.tests.script_semantics_fixtures import script_example
from studio.tests.test_script_semantic_requests import reference as output_reference

PROJECT_CONFIG = {"language": "English", "aspect_ratio": "9:16", "width": 720, "height": 1280,
    "fps": 24, "default_shot_duration_ms": 1000, "continuity_seed": 5}


class ExecutionContextInputTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="context-owner")
        self.other = get_user_model().objects.create_user(username="context-other")
        self.project = SeriesProject.objects.create(owner=self.owner, title="Context project", **PROJECT_CONFIG)
        self.source, preview = create_pasted_script_source(project=self.project, user=self.owner, content="旅人走进房间。\n旅人放下盒子。")
        self.revision = confirm_script_revision(preview, user=self.owner)
        self.spec = {"key": "script", "kind": "text", "required": True, "min_items": 1, "max_items": 1,
            "semantic_contract": {"schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": [],
                "allowed_scope_kinds": ["project", "request"], "paired_with_input_key": None, "allow_repeated_asset": False},
            "targets": [{"target_kind": "operation_input", "target_key": "source_text", "is_primary": True}]}
        self.definition = json_contract({"schema_version": 1, "configuration": {"operation": "script.structure", "inputs": [self.spec]},
            "resources": {"conditioning_mode": None}, "interface": {"input_targets": {"source_text": {
                "target_kind": "operation_input", "target_key": "source_text", "kind": "text", "required": True, "min_items": 1, "max_items": 1}}}})
        self.input = {"kind": "context_field", "selector_key": "script.content", "scope": {"kind": "project", "id": str(self.project.pk)},
            "content_revision_id": str(self.revision.pk)}

    def resolve(self, source=None, **kwargs):
        return resolve_execution_input_sources(self.definition, {"script": self.input if source is None else source},
            owner=kwargs.pop("owner", self.owner), subject_facts=[], max_media_bytes=65536, **kwargs).value

    def test_actual_source_is_frozen_and_not_replaced_with_latest_revision(self):
        result = self.resolve(project_id=self.project.pk)
        self.assertEqual(result["operation_inputs"]["source_text"], self.revision.content)
        origin = result["input_provenance"][0]
        self.assertEqual(origin["content_revision_id"], str(self.revision.pk))
        self.assertEqual(origin["confirmed_by_id"], str(self.owner.pk))
        reference = native_text_source_reference(self.definition.value, result)
        self.assertEqual(reference["id"], str(self.revision.pk))
        _, structure, mapping = script_example()
        interface = {"operation": "script.structure", "output": {"adapter": "text.script_structure/v3"},
            "business_output": output_reference(structure, mapping),
            "native_policy": {"timeout_seconds": 30, "chat_request_options": {}}, "model_identifier": "test-model"}
        first = build_native_text_plan(interface, inputs=result["operation_inputs"], parameters={"max_tokens": 2048, "seed": 42}, reference_max_bytes=65536, source_reference=reference)
        first["prompt_profile_assembly"] = {"channels": {"positive_prompt": "本次结构化要求"}, "structured_fields": {}}
        payload, envelope = compile_script_semantic_plan(operation="script.structure", plan=first,
            plan_max_bytes=65536, request_max_bytes=65536)
        next_revision = create_script_revision(source=self.source, user=self.owner, content="新内容")
        confirm_script_revision(next_revision, user=self.owner)
        with self.assertRaises(ValidationError):
            self.resolve()
        with self.assertNumQueries(0):
            frozen_output = verify_script_semantic_request(envelope, payload, max_bytes=65536,
                expected_output=interface["business_output"])
            replay = compile_script_semantic_plan(operation="script.structure", plan=first,
                plan_max_bytes=65536, request_max_bytes=65536)
        self.assertEqual(frozen_output, interface["business_output"])
        self.assertEqual(replay, (payload, envelope))
        self.assertEqual(first["source_kind"], "script_revision")
        self.assertEqual(first["source_revision"]["id"], str(self.revision.pk))

    def test_catalog_scope_ownership_and_confirmed_status_cannot_be_forged(self):
        preview = create_script_revision(source=self.source, user=self.owner, content="未确认文本")
        cases = [{**self.input, "content_revision_id": str(preview.pk)}, {**self.input, "selector_key": "script.__dict__"},
            {**self.input, "content": "客户端替换正文"}, {**self.input, "scope": {"kind": "request"}},
            {**self.input, "content_revision_id": None}]
        for source in cases:
            with self.subTest(source=source), self.assertRaises(ValidationError):
                self.resolve(source)
        with self.assertRaises(ValidationError):
            self.resolve(owner=self.other)
        another = SeriesProject.objects.create(owner=self.owner, title="Other project", **PROJECT_CONFIG)
        with self.assertRaises(ValidationError):
            self.resolve(project_id=another.pk)
        ScriptRevision.objects.filter(pk=self.revision.pk).update(content="isolated corrupt source")
        with self.assertRaises(ValidationError):
            self.resolve()

    def test_literal_is_explicit_and_has_no_revision_or_confirmation(self):
        result = self.resolve({"kind": "literal", "value": "本次输入"})
        self.assertIsNone(native_text_source_reference(self.definition.value, result))
        self.assertEqual(set(result["input_provenance"][0]), {"input_key", "kind", "value_sha256"})
        missing = deepcopy(result)
        missing["input_provenance"] = []
        with self.assertRaises(ValidationError):
            native_text_source_reference(self.definition.value, missing)

    def test_source_reads_are_bounded_and_use_exact_object_indexes(self):
        from studio.services.execution_input_resolution import _context_field
        with CaptureQueriesContext(connection) as queries:
            _context_field(self.input, spec=self.spec, owner=self.owner, project_id=self.project.pk,
                production_run_id=None, eligible_stage_ids=(), context_cache={}, production_scope=None)
        self.assertEqual(len(queries), 2)
        for query in queries:
            self.assertNotIn('SELECT *', query['sql'].upper())
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                for query in queries:
                    cursor.execute('EXPLAIN QUERY PLAN ' + query['sql'])
                    plan = ' '.join(str(row[-1]) for row in cursor.fetchall()).upper()
                    self.assertIn('SEARCH', plan)
                    self.assertNotIn('SCAN ', plan)
                    self.assertNotIn('TEMP B-TREE', plan)

    def test_project_style_is_precise_current_fact_not_default_or_bible_context(self):
        payload = deepcopy(self.definition.value)
        payload["configuration"]["inputs"][0].update(kind="json", targets=[])
        definition = json_contract(payload)
        source = {**self.input, "selector_key": "script.project_style"}
        def read():
            return resolve_execution_input_sources(definition, {"script": source}, owner=self.owner,
                subject_facts=[], max_media_bytes=65536, project_id=self.project.pk).value
        self.assertIsNone(read()["logical_values"]["script"])
        style = Style.objects.create(owner=self.owner, slug="reviewed-project-style", name="冷色写实", category_id="drama",
            description="低饱和", visual_prompt="soft light", negative_prompt="overexposure")
        self.project.style = style; self.project.save(update_fields=("style",))
        frozen = read()
        self.assertEqual(frozen["logical_values"]["script"], {**{key: getattr(style, key) for key in
            ("name", "description", "visual_prompt", "negative_prompt")}, "category": "drama", "category_label": "剧情"})
        self.assertEqual(frozen["input_provenance"][0]["content_revision_id"], str(self.revision.pk))
        self.assertNotIn("review", frozen["input_provenance"][0])
        style.visual_prompt = "new project setting"; style.save(update_fields=("visual_prompt",))
        self.assertEqual(frozen["logical_values"]["script"]["visual_prompt"], "soft light")
        self.assertEqual(read()["logical_values"]["script"]["visual_prompt"], "new project setting")
