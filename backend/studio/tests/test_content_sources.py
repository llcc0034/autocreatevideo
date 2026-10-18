from studio.tests.field_definition_fixtures import create_field_definition
from copy import deepcopy
import importlib
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import ContentSourceVersion, PlatformLifecycleAuditEvent, FieldDefinition, PromptProfileFieldRule, DataStructureVersion
from studio.platform_serializers import PromptProfileFieldRuleSerializer
from studio.services.content_sources import source_reference, source_reader_options, write_source, verified_source_contract, verify_source_reference, validate_source_value
from studio.services.data_structures import write_data_structure, transition_data_structure
from studio.services.json_contracts import json_contract
from studio.services.execution_source_readers import REQUEST_TEXT_SOURCE_SELECTORS, input_source_reader, validate_source_reader
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile, verify_prompt_profile_contract, clone_prompt_profile_successor
from studio.services.execution_definitions import build_execution_definition, read_execution_input_contract
from studio.services.execution_instances import publish_execution_version
from studio.services.execution_input_suggestions import preview_execution_input_suggestion
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_profile_inputs import project_execution_profile_sources
from studio.services.prompt_profile_compiler import PromptProfileCompilationFacts, compile_prompt_profile_facts
from studio.tests import test_execution_project_sources as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ContentSourceTests(TestCase):
    write_contract = staticmethod(fixtures.ExecutionProjectSourceTests.write_contract)
    setUpTestData = classmethod(fixtures.ExecutionProjectSourceTests.setUpTestData.__func__)
    setUp = fixtures.ExecutionProjectSourceTests.setUp
    profile = fixtures.ExecutionProjectSourceTests.profile
    configure = fixtures.ExecutionProjectSourceTests.configure

    def structure_id(self):
        if not hasattr(self, "_structure_id"):
            definition = create_field_definition(key="source_" + uuid4().hex, value_type="long_text")
            structure = write_data_structure(content={"name": "Source text", "nodes": [
                {"node_key": "root", "field_definition_version": str(definition._fixture_version.pk)}]})
            self._structure_id = str(transition_data_structure(version_id=structure.pk, status="published").pk)
        return self._structure_id

    def custom_structure(self, kind="long_text", *, publish=True, **node_values):
        definition = create_field_definition(key="custom_source_" + uuid4().hex, value_type=kind)
        structure = write_data_structure(content={"name": "Configured source value", "nodes": [
            {"node_key": "value", "field_definition_version": str(definition._fixture_version.pk), **node_values}]})
        return transition_data_structure(version_id=structure.pk, status="published") if publish else structure

    def source(self, *, selector="project.logline", publish=True):
        source = write_source(actor=self.actor, data={"name": "创作梗概", "reader_selector": selector,
            "data_structure_version": self.structure_id()})
        return write_source(actor=self.actor, source_id=source.pk, expected_revision=1, action="publish") if publish else source

    def test_version_lifecycle_is_immutable_audited_and_stale_edits_rejected(self):
        source = self.source()
        original = source_reference(source)
        for action, revision in (("update", 2), ("publish", 2), ("retire", 1)):
            with self.subTest(action=action), self.assertRaises(ValidationError):
                write_source(actor=self.actor, source_id=source.pk, expected_revision=revision, action=action)
        successor = write_source(actor=self.actor, source_id=source.pk, expected_revision=2, action="successor")
        self.assertEqual(successor.version, 2)
        successor = write_source(actor=self.actor, source_id=successor.pk, expected_revision=1,
            data={"name": "新版梗概", "reader_selector": "style.visual_prompt"})
        self.assertNotEqual(successor.contract_sha256, source.contract_sha256)
        source.refresh_from_db()
        self.assertEqual(source_reference(source), original)
        with self.assertRaises(ValidationError):
            write_source(actor=self.actor, source_id=source.pk, expected_revision=2, action="successor")
        source = write_source(actor=self.actor, source_id=source.pk, expected_revision=2, action="retire")
        self.assertEqual(source_reference(source), original)
        with self.assertRaises(ValidationError): source_reference(source, require_published=True)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.filter(object_type="content_source_version").count(), 5)

    def test_reader_catalog_is_finite_and_does_not_allow_arbitrary_paths(self):
        options = source_reader_options()
        self.assertEqual(len(options), 25)
        self.assertEqual(sum(row["reader"]["scope_kind"] == "project" for row in options), 6)
        camera = next(row for row in options if row["reader"]["selector"] == "reviewed_visual_rules.camera_guidance")
        self.assertEqual(camera["reader"]["output_field"], "camera_guidance")
        self.assertEqual(camera["reader"]["snapshot_policy"], "exact_current_approval")
        request_options = [row for row in options if row["reader"]["protocol"] == "request_text/v1"]
        self.assertEqual({row["value"] for row in request_options}, set(REQUEST_TEXT_SOURCE_SELECTORS))
        self.assertTrue(all(row["reader"]["scope_kind"] == "request" and row["reader"]["value_kind"] == "text" for row in request_options))
        for selector in ("project.master_script", "https://example.com", "project.owner.password", "os.system", "",
                "request.owner.password", "request.reference_assets", "request.new_field"):
            with self.subTest(selector=selector), self.assertRaises(ValidationError): self.source(selector=selector)
        self.assertFalse(ContentSourceVersion.objects.exists())

    def test_request_source_versions_derive_single_ports_and_freeze_only_explicit_text(self):
        theme = self.source(selector="request.theme")
        style = self.source(selector="request.style")
        profile = self.profile(publish=False)
        rule = profile.field_rules.get()
        serializer = PromptProfileFieldRuleSerializer(rule, data={"source_version": str(theme.pk)}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        for position, (key, source) in enumerate((("source_style", style), ("theme_second_use", theme)), start=2):
            field = create_field_definition(key=key, label=key, value_type="long_text")
            create_profile_field_rule(profile_version=profile, field_definition=field,
                source_version=source, source_selector=source.reader_selector, generation_source_selector=source.reader_selector,
                generation_mode="passthrough", channel="positive_prompt", position=position, required=True, max_chars=4000)
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        self.configure(profile, [])
        definition = build_execution_definition(self.version.pk)
        self.assertEqual(len(definition.value["configuration"]["inputs"]), 2, "same source has only one confirmation input")
        for spec in definition.value["configuration"]["inputs"]:
            self.assertEqual(spec["semantic_contract"]["allowed_scope_kinds"], ["request"])
            target = definition.value["interface"]["profile_inputs"][spec["key"]]
            self.assertEqual(target["source_kinds"], ["literal"])
            self.assertEqual(target["source_reader"]["protocol"], "request_text/v1")
            self.assertTrue(target["source_definitions"][0]["sha256"])
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        contract = read_execution_input_contract(self.version.pk)
        self.assertEqual(contract["input_forms"]["request.theme"]["label"], theme.name)
        values = {"request.theme": "寻找故人 A short logline.", "request.style": "自然光 natural cinematic"}
        sources = {key: {"kind": "literal", "value": value} for key, value in values.items()}
        with patch("studio.services.execution_project_sources.authorized_project", side_effect=AssertionError("No implicit project read")):
            resolved = resolve_execution_input_sources(definition, sources, owner=self.actor,
                subject_facts=[], max_media_bytes=65536)  # standalone Smoke need not invent a project
        projected = project_execution_profile_sources(definition, resolved.value["logical_values"])
        self.assertEqual(projected["assembly_sources"], values)
        compiled = compile_prompt_profile_facts(profile, facts=PromptProfileCompilationFacts(sources=projected["assembly_sources"],
            source_language="Chinese", target_language="Chinese", reference_context=None,
            reviewed_values={}, generation_contributions={}))
        for value in values.values(): self.assertIn(value, compiled.assembly["channels"]["positive_prompt"])
        self.assertNotIn("DO NOT READ", str(compiled.assembly))
        self.assertTrue(all(row["kind"] == "literal" for row in resolved.value["input_provenance"]))
        sources["request.theme"]["value"] = "later edit"
        self.assertEqual(resolved.value["logical_values"], values)
        for bad in ({"kind": "context_field"}, {"kind": "project_snapshot"}, {"kind": "selected_output"},
                {"kind": "literal", "value": 123}, {"kind": "literal", "value": "text", "reference": "forged"}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                resolve_execution_input_sources(definition, {**sources, "request.theme": bad}, owner=self.actor,
                    subject_facts=[], max_media_bytes=65536)
        with self.assertRaises(ValidationError):
            resolve_execution_input_sources(definition, {}, owner=self.actor, subject_facts=[], max_media_bytes=65536)
        original = deepcopy(verify_prompt_profile_contract(profile))
        successor = write_source(actor=self.actor, source_id=theme.pk, expected_revision=2, action="successor")
        write_source(actor=self.actor, source_id=successor.pk, expected_revision=1, data={"reader_selector": "project.logline"})
        profile.refresh_from_db()
        self.assertEqual(verify_prompt_profile_contract(profile), original)
        self.assertEqual(read_execution_input_contract(self.version.pk)["sha256"], contract["sha256"])

    def test_all_request_readers_are_formally_authored_and_scope_or_type_cannot_be_forged(self):
        self.actor.is_staff = True; self.actor.save(update_fields=["is_staff"])
        api = APIClient(); api.force_authenticate(self.actor)
        url = "/api/v1/content-source-versions/"
        self.assertEqual(len(api.get(url + "readers/").data), 25)
        for selector in REQUEST_TEXT_SOURCE_SELECTORS:
            response = api.post(url, {"name": selector, "reader_selector": selector,
                "data_structure_version": self.structure_id()}, format="json")
            self.assertEqual(response.status_code, 201, response.data)
            response = api.post(url + response.data["id"] + "/publish/", {"expected_revision": 1}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            reader = response.data["reader"]
            self.assertEqual(reader["snapshot_policy"], "confirmed_request")
            for changes in ({"scope_kind": "project"}, {"value_kind": "number"}, {"snapshot_policy": "latest"}):
                with self.assertRaises(ValidationError): validate_source_reader({**reader, **changes})
            spec = {"key": selector, "targets": [{"target_kind": "profile_input", "target_key": selector}],
                "kind": "text", "semantic_contract": {"allowed_scope_kinds": ["project"]}}
            with self.assertRaises(ValidationError):
                input_source_reader(spec, {"profile_inputs": {selector: {"source_reader": reader}}})
        profile = self.profile(value_type="number", publish=False)
        source = ContentSourceVersion.objects.get(reader_selector="request.theme")
        serializer = PromptProfileFieldRuleSerializer(profile.field_rules.get(), data={"source_version": str(source.pk)}, partial=True)
        self.assertFalse(serializer.is_valid(), "text reader must not silently become numeric")

    def test_failed_audit_rolls_back_source_and_published_contract_detects_tampering(self):
        with patch("studio.services.content_sources.record_platform_lifecycle_event", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError): self.source()
        self.assertFalse(ContentSourceVersion.objects.exists())
        source = self.source()
        ContentSourceVersion.objects.filter(pk=source.pk).update(reader_selector="style.visual_prompt")
        source.refresh_from_db()
        with self.assertRaises(ValidationError): source_reference(source)

    def test_formal_api_permissions_pagination_unknown_fields_and_revision(self):
        api = APIClient()
        url = "/api/v1/content-source-versions/"
        self.assertIn(api.get(url).status_code, (401, 403))
        api.force_authenticate(get_user_model().objects.create_user(username="ordinary-source-user"))
        self.assertEqual(api.post(url, {"name": "x"}).status_code, 403)
        self.actor.is_staff = True; self.actor.save(update_fields=["is_staff"])
        api.force_authenticate(self.actor)
        self.assertEqual(api.post(url, {"name": "x", "reader_selector": "project.logline", "status": "published"}, format="json").status_code, 400)
        response = api.post(url, {"name": "Api source", "reader_selector": "project.logline",
            "data_structure_version": self.structure_id()}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        detail = url + str(response.data["id"]) + "/"
        self.assertEqual(api.patch(detail, {"name": "miss revision"}, format="json").status_code, 400)
        self.assertEqual(api.post(detail + "publish/", {"expected_revision": 1, "name": "unsaved"}, format="json").status_code, 400)
        self.assertEqual(api.post(detail + "publish/", {"expected_revision": 1}, format="json").status_code, 200)
        response = api.get(url, {"status": "published", "search": "Api", "page_size": 99999})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(api.delete(detail).status_code, 405)

    def test_exact_definition_is_frozen_in_strategy_and_execution_and_uses_original_permission_reader(self):
        source = self.source()
        profile = self.profile(selector="project.logline", publish=False)
        rule = profile.field_rules.get()
        serializer = PromptProfileFieldRuleSerializer(rule, data={"source_version": str(source.pk)}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        profile.refresh_from_db()
        profile = publish_prompt_profile(profile, actor=self.actor)
        payload = deepcopy(verify_prompt_profile_contract(profile))
        self.assertEqual(payload["rules"][0]["source_definition"]["sha256"], source.contract_sha256)
        self.configure(profile, [])
        definition = build_execution_definition(self.version.pk)
        self.assertEqual(definition.value["interface"]["profile_inputs"]["project.logline"]["source_definitions"][0]["id"], str(source.pk))
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        contract = read_execution_input_contract(self.version.pk)
        self.assertEqual(contract["input_forms"]["project.logline"]["label"], source.name)
        preview = preview_execution_input_suggestion(contract, owner=self.actor, project_id=self.project.pk,
            input_key="project.logline", suggestion_key="project.logline")
        reference = {"project.logline": {"kind": "project_snapshot", "reference": preview["reference"]}}
        resolved = resolve_execution_input_sources(definition, reference, owner=self.actor, subject_facts=[], max_media_bytes=65536, project_id=self.project.pk)
        self.assertEqual(project_execution_profile_sources(definition, resolved.value["logical_values"])["assembly_sources"]["project.logline"], self.project.logline)
        stranger = get_user_model().objects.create_user(username="stranger-source")
        with self.assertRaises(ValidationError):
            resolve_execution_input_sources(definition, reference, owner=stranger, subject_facts=[], max_media_bytes=65536, project_id=self.project.pk)
        write_source(actor=self.actor, source_id=source.pk, expected_revision=2, action="retire")
        profile.refresh_from_db()
        self.assertEqual(verify_prompt_profile_contract(profile), payload)
        successor = clone_prompt_profile_successor(profile, expected_source_contract_sha256=profile.contract_sha256, name="Replace retired source")
        self.assertEqual(successor.field_rules.get().source_version_id, source.pk)
        successor.review_state = "ready"; successor.save(update_fields=["review_state"])
        with self.assertRaises(ValidationError): publish_prompt_profile(successor, actor=self.actor)
        with self.assertRaises(ProtectedError): source.delete()

    def test_rule_rejects_draft_wrong_type_wrong_mapping_and_retired_source(self):
        source = self.source(publish=False)
        profile = self.profile(selector="project.logline", publish=False)
        rule = profile.field_rules.get()
        serializer = PromptProfileFieldRuleSerializer(rule, data={"source_version": str(source.pk)}, partial=True)
        self.assertFalse(serializer.is_valid())
        source = write_source(actor=self.actor, source_id=source.pk, expected_revision=1, action="publish")
        for changes in ({"source_selector": "request.theme"}, {"generation_mode": "model_generated"}, {"channel": "system_instruction"}):
            rule.refresh_from_db()
            serializer = PromptProfileFieldRuleSerializer(rule, data={"source_version": str(source.pk), **changes}, partial=True)
            self.assertFalse(serializer.is_valid(), changes)
        write_source(actor=self.actor, source_id=source.pk, expected_revision=2, action="retire")
        rule.refresh_from_db()
        serializer = PromptProfileFieldRuleSerializer(rule, data={"source_version": str(source.pk)}, partial=True)
        self.assertFalse(serializer.is_valid())

    def test_registry_query_is_bounded_narrow_and_indexed(self):
        self.source()
        query = ContentSourceVersion.objects.filter(status="published").order_by("-created_at", "id")[:20]
        with CaptureQueriesContext(connection) as captured: list(query)
        self.assertEqual(len(captured), 1)
        self.assertNotIn("master_script", captured[0]["sql"])
        if connection.vendor == "sqlite": self.assertIn("studio_source_status_idx", query.explain())

    def test_exact_text_source_can_feed_generation_and_translation_without_losing_provenance(self):
        source = self.source(selector="request.theme")
        profile = self.profile(publish=False)
        rule = profile.field_rules.get()
        for mode in ("model_generated", "translate_only"):
            serializer = PromptProfileFieldRuleSerializer(rule, data={
                "source_version": str(source.pk), "generation_mode": mode,
                "generation_instruction": "Use only the exact supplied source.",
                "generation_output_semantics": "final", "generation_required": True,
            }, partial=True)
            self.assertTrue(serializer.is_valid(), serializer.errors)
            rule = serializer.save()
        profile.refresh_from_db()
        profile = publish_prompt_profile(profile, actor=self.actor)
        from studio.services.execution_profile_inputs import profile_input_targets
        target = profile_input_targets(profile)["request.theme"]
        self.assertEqual(target["fields"][0]["usage"], "generation_source")
        self.assertEqual(target["source_definitions"][0]["id"], str(source.pk))
        frozen_rule = verify_prompt_profile_contract(profile)["rules"][0]
        self.assertEqual(frozen_rule["source_definition"]["sha256"], source.contract_sha256)
        self.assertEqual(frozen_rule["generation_source_selector"], "request.theme")

    def test_structure_is_explicit_typed_and_frozen_without_live_registry_reads(self):
        for structure in (None, self.custom_structure(publish=False), self.custom_structure("integer")):
            data = {"name": "Invalid", "reader_selector": "request.theme"}
            if structure:
                data["data_structure_version"] = str(structure.pk)
            with self.subTest(structure=structure), self.assertRaises(ValidationError):
                write_source(actor=self.actor, data=data)
        self.assertFalse(ContentSourceVersion.objects.exists())
        structure = self.custom_structure(allow_blank=False, constraints={"max_length": 3})
        source = write_source(actor=self.actor, data={"name": "Short text", "reader_selector": "request.theme",
            "data_structure_version": str(structure.pk)})
        source = write_source(actor=self.actor, source_id=source.pk, expected_revision=1, action="publish")
        frozen = source_reference(source)
        self.assertEqual(frozen["schema_version"], 2)
        self.assertEqual(frozen["data_structure"]["id"], str(structure.pk))
        self.assertNotIn("json_schema", frozen)
        with self.assertNumQueries(0):
            self.assertEqual(verify_source_reference(frozen), frozen)
            validate_source_value(frozen, "短文", path="input.theme")
            for value in ("", "abcd", 0, False, None, []):
                with self.subTest(value=value), self.assertRaises(ValidationError) as caught:
                    validate_source_value(frozen, value, path="input.theme")
                self.assertIn("input.theme", caught.exception.message_dict)
        newer = write_data_structure(source_version_id=structure.pk, content={"name": "New meaning"})
        self.assertNotEqual(str(newer.pk), frozen["data_structure"]["id"])
        successor = write_source(actor=self.actor, source_id=source.pk, expected_revision=2, action="successor")
        self.assertEqual(successor.data_structure_version_id, structure.pk)
        transition_data_structure(version_id=structure.pk, status="retired")
        with self.assertRaises(ValidationError):
            write_source(actor=self.actor, source_id=successor.pk, expected_revision=1, action="publish")
        source.refresh_from_db()
        self.assertEqual(source_reference(source), frozen)
        with self.assertRaises(ProtectedError): structure.delete()
        corrupt = deepcopy(frozen)
        corrupt["data_structure"]["nodes"][0]["constraints"]["max_length"] = 30
        with self.assertRaises(ValidationError): verify_source_reference(corrupt)
        old = {key: value for key, value in frozen.items() if key != "data_structure"}
        old["schema_version"] = 1
        with self.assertRaises(ValidationError): verify_source_reference(old)

    def test_frozen_structure_constraints_apply_to_resolver_and_profile_projection(self):
        self._structure_id = str(self.custom_structure(constraints={"max_length": 3}).pk)
        source = self.source(selector="request.theme")
        profile = self.profile(publish=False)
        rule = profile.field_rules.get()
        rule.source_version = source; rule.save(update_fields=["source_version"])
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        self.configure(profile, [])
        definition = build_execution_definition(self.version.pk)
        resolved = resolve_execution_input_sources(definition, {"request.theme": {"kind": "literal", "value": "abc"}},
            owner=self.actor, subject_facts=[], max_media_bytes=65536)
        self.assertEqual(resolved.value["logical_values"]["request.theme"], "abc")
        self.assertEqual(project_execution_profile_sources(definition, {"request.theme": "abc"})["assembly_sources"], {"request.theme": "abc"})
        with self.assertRaises(ValidationError) as caught:
            resolve_execution_input_sources(definition, {"request.theme": {"kind": "literal", "value": "abcd"}},
                owner=self.actor, subject_facts=[], max_media_bytes=65536)
        self.assertIn("inputs.request.theme", caught.exception.message_dict)
        with self.assertRaises(ValidationError): project_execution_profile_sources(definition, {"request.theme": "abcd"})

    def test_api_requires_structure_and_list_does_not_load_each_structure(self):
        self.actor.is_staff = True; self.actor.save(update_fields=["is_staff"])
        api = APIClient(); api.force_authenticate(self.actor)
        url = "/api/v1/content-source-versions/"
        response = api.post(url, {"name": "No structure", "reader_selector": "request.theme"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("data_structure_version", response.data["error"]["details"])
        for _ in range(3): self.source()
        with CaptureQueriesContext(connection) as queries:
            response = api.get(url)
        self.assertEqual(response.status_code, 200)
        relevant = [row["sql"] for row in queries if "studio_contentsourceversion" in row["sql"] or "studio_datastructure" in row["sql"]]
        self.assertEqual(len(relevant), 2, relevant)
        self.assertNotIn("reader_contract", relevant[-1])
        self.assertNotIn("studio_datastructurenode", str(relevant))
        self.assertEqual(str(response.data["results"][0]["data_structure_version"]), self.structure_id())

    def test_saved_reader_survives_catalog_changes_and_successor_does_not_refresh_it(self):
        source = self.source(publish=False)
        original = deepcopy(source.reader_contract)
        with patch("studio.services.content_sources.reader_contract", side_effect=AssertionError("No current catalog")):
            source = write_source(actor=self.actor, source_id=source.pk, expected_revision=1,
                data={"name": "改业务名称", "reader_selector": source.reader_selector})
            source = write_source(actor=self.actor, source_id=source.pk, expected_revision=2, action="publish")
            successor = write_source(actor=self.actor, source_id=source.pk, expected_revision=3, action="successor")
            self.assertEqual(source_reference(source)["reader"], original)
            self.assertEqual(successor.reader_contract, original)
            self.actor.is_staff = True; self.actor.save(update_fields=["is_staff"])
            api = APIClient(); api.force_authenticate(self.actor)
            response = api.get(f"/api/v1/content-source-versions/{source.pk}/")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["reader"], original)
        updated = write_source(actor=self.actor, source_id=successor.pk, expected_revision=1,
            data={"reader_selector": "style.visual_prompt"})
        self.assertEqual(updated.reader_contract["selector"], "style.visual_prompt")
        source.refresh_from_db()
        self.assertEqual(source.reader_contract, original)

    def test_corrupt_draft_cannot_be_rehashed_by_save_or_publish_and_api_reports_failure(self):
        source = self.source(publish=False)
        before = ContentSourceVersion.objects.values().get(pk=source.pk)
        audits = PlatformLifecycleAuditEvent.objects.count()
        for changes in ({"reader_contract": {}}, {"reader_contract": {**source.reader_contract, "scope_kind": "request"}},
                {"reader_selector": "style.visual_prompt"}, {"description": "tampered"}, {"contract_sha256": "0" * 64}):
            with self.subTest(changes=changes):
                ContentSourceVersion.objects.filter(pk=source.pk).update(**changes)
                corrupted = ContentSourceVersion.objects.values().get(pk=source.pk)
                for action in ("update", "publish", "retire", "successor"):
                    with self.assertRaises(ValidationError):
                        write_source(actor=self.actor, source_id=source.pk, expected_revision=1, action=action)
                self.actor.is_staff = True; self.actor.save(update_fields=["is_staff"])
                api = APIClient(); api.force_authenticate(self.actor)
                self.assertEqual(api.get(f"/api/v1/content-source-versions/{source.pk}/").status_code, 400)
                summary = api.get("/api/v1/content-source-versions/")
                self.assertEqual(summary.status_code, 200)
                self.assertNotIn("reader", summary.data["results"][0])
                self.assertEqual(ContentSourceVersion.objects.values().get(pk=source.pk), corrupted)
                ContentSourceVersion.objects.filter(pk=source.pk).update(**{key: before[key] for key in changes})
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), audits)
        for revision in (True, 1.0, "1", None):
            with self.assertRaises(ValidationError):
                write_source(actor=self.actor, source_id=source.pk, expected_revision=revision, data={"name": "reject"})

    def test_profile_ports_and_runtime_reader_use_frozen_definition_not_author_directory(self):
        from studio.services.execution_profile_inputs import profile_input_targets
        source = self.source(selector="reviewed_visual_rules.color_script")
        profile = self.profile(selector="reviewed_visual_rules.color_script", publish=False)
        rule = profile.field_rules.get()
        rule.source_version = source; rule.save(update_fields=["source_version"])
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        key = source.reader_selector
        with patch("studio.services.content_sources.reader_contract", side_effect=AssertionError("No current author catalog")), \
                patch("studio.services.execution_source_readers.reviewed_visual_source_reader", side_effect=AssertionError("No projection rebuild")):
            targets = profile_input_targets(profile)
            reader = targets[key]["source_reader"]
            self.assertEqual(reader, source.reader_contract)
            spec = {"key": key, "targets": [{"target_kind": "profile_input", "target_key": key}],
                "kind": "text", "semantic_contract": {"allowed_scope_kinds": ["project"]}}
            self.assertEqual(input_source_reader(spec, {"profile_inputs": targets}), reader)
        for changes in ({"output_schema_version": 3.0}, {"output_field": "continuity_summary"},
                {"protocol": "custom/url"}, {"path": "project.owner.password"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_source_reader({**reader, **changes})
        with self.assertRaises(ValidationError):
            input_source_reader({**spec, "kind": "json"}, {"profile_inputs": targets})

    def test_reader_materialization_preserves_facts_and_rolls_back_on_unreproducible_digest(self):
        migration = importlib.import_module("studio.migrations.0188_freeze_content_source_reader")
        # 0188 materializes the historical five protocols, not future catalog entries.
        rows = [self.source(selector=selector) for selector in ("project.logline", "style.visual_prompt",
            "reviewed_visual_rules.continuity_summary", "reviewed_visual_rules.color_script", "reviewed_visual_rules.negative_prompt")]
        # Isolated historical v1 envelopes, not a compatibility path for current sources.
        for row in rows:
            reference = source_reference(row)
            contract = json_contract({**{key: value for key, value in reference.items()
                if key not in {"data_structure", "sha256"}}, "schema_version": 1})
            ContentSourceVersion.objects.filter(pk=row.pk).update(
                contract_sha256=contract.sha256, contract_size_bytes=contract.size_bytes, data_structure_version=None)
        before = list(ContentSourceVersion.objects.order_by("id").values())
        audits = PlatformLifecycleAuditEvent.objects.count()
        apps = SimpleNamespace(get_model=lambda *_: ContentSourceVersion)
        editor = SimpleNamespace(connection=connection)
        ContentSourceVersion.objects.update(reader_contract={})
        with patch("studio.services.content_sources.reader_contract", side_effect=AssertionError("No runtime catalog")):
            migration.freeze_readers(apps, editor)
        self.assertEqual(list(ContentSourceVersion.objects.order_by("id").values()), before)
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.reader_contract["selector"], row.reader_selector)
        ContentSourceVersion.objects.update(reader_contract={})
        # Migration ordering is newest first: force an earlier write before failure.
        ContentSourceVersion.objects.filter(pk=rows[0].pk).update(contract_sha256="0" * 64)
        with self.assertRaises(RuntimeError), transaction.atomic():
            migration.freeze_readers(apps, editor)
        self.assertEqual(list(ContentSourceVersion.objects.values_list("reader_contract", flat=True)), [{}] * 5)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), audits)
