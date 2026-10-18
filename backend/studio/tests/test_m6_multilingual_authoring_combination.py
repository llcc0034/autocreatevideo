"""Batch 342 M6-A01: HTTP authors/Shot choices -> isolated frozen request.

SQLite in-memory and temporary managed PNGs only. The published resource and
admission observations are fixtures, not live admission or Smoke. Authoring,
Run/storyboard review, exact Shot selection and field confirmation use formal
HTTP APIs. Field evaluation and ORIGINAL INTERNAL finalize_execution are local
service boundaries; neither main nor auxiliary execution is performed.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import (
    CharacterBible, ModelExecutionInstanceVersion, ProductionManualCommand,
    PromptProfileFieldRule, PromptProfileVersion, ShotReferenceUse,
)
from studio.services.bible_reference_contract import bible_reference_name_key
from studio.services.json_contracts import json_contract
from studio.services.production_reference_mapping import (
    expand_reviewed_reference_sources, reference_use_records,
)
from studio.services.shot_reference_sources import resolve_shot_reference_sources
from studio.tests import test_m6_production_combination_acceptance as combinations
from studio.tests import test_m6_reference_acceptance as references
from studio.tests.test_production_reference_mapping import mapping


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class M6MultilingualAuthoringCombinationTests(TestCase):
    # Bind helpers only: do not inherit/discover the old A02/A03 or A01 tests.
    _create_version = combinations.M6ProductionCombinationAcceptanceTests._create_version
    selection = combinations.M6ProductionCombinationAcceptanceTests.selection
    configure = combinations.M6ProductionCombinationAcceptanceTests.configure
    setting = combinations.M6ProductionCombinationAcceptanceTests.setting
    reference_use = combinations.M6ProductionCombinationAcceptanceTests.reference_use
    author_target = combinations.M6ProductionCombinationAcceptanceTests.author_target
    post = combinations.M6ProductionCombinationAcceptanceTests.post
    read = combinations.M6ProductionCombinationAcceptanceTests.read
    adopt_source = combinations.M6ProductionCombinationAcceptanceTests.adopt_source
    approval = combinations.M6ProductionCombinationAcceptanceTests.approval
    reviewed_production_action = combinations.M6ProductionCombinationAcceptanceTests.reviewed_production_action
    create_manual_preparation = combinations.M6ProductionCombinationAcceptanceTests.create_manual_preparation
    assert_frozen_reads_and_replay = combinations.M6ProductionCombinationAcceptanceTests.assert_frozen_reads_and_replay
    call_counts = staticmethod(combinations.M6ProductionCombinationAcceptanceTests.call_counts)

    def setUp(self):
        combinations.M6ProductionCombinationAcceptanceTests.setUp(self)
        self.assertEqual(len(self.guards), 13)
        self.api_trace = []
        self.field_receipts = []
        references.M6ReferenceAcceptanceTests.setUp(self)
        self.media_root = Path(settings.MEDIA_ROOT).resolve()
        self.assertNotEqual(self.media_root, Path(settings.BASE_DIR, "media").resolve())
        self.assertNotEqual(self.media_root, Path("E:/aicoding/autoprompt/backend/media").resolve())
        for asset in self.assets:
            self.assertTrue(Path(asset.file.path).resolve().is_relative_to(self.media_root))

    tearDown = combinations.M6ProductionCombinationAcceptanceTests.tearDown

    def api(self, method, path, body=None, *, status=200, **headers):
        result = combinations.M6ProductionCombinationAcceptanceTests.api(
            self, method, path, body, status=status, **headers)
        self.api_trace.append({"method": method.upper(), "path": path, "status": status})
        if method == "post" and path.startswith("/api/v1/execution-field-values/"):
            self.field_receipts.append({"path": path, "request": deepcopy(body), "response": deepcopy(result)})
        return result

    def assert_no_writes(self, queries):
        writes = [row["sql"] for row in queries if row["sql"].lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP"))]
        self.assertEqual(writes, [])

    def bible_rows(self):
        return list(CharacterBible.objects.filter(pk__in=[row.pk for row in self.entities])
            .order_by("slug").values("id", "slug", "name", "description", "stable_speaker_id", "updated_at"))

    def author_exact_chain(self):
        """Extend the 338 author sequence with an API-authored exact structure."""
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        profiles = "/api/v1/prompt-profile-versions/"
        source = self.api("get", f"{profiles}{self.prompt_profile.pk}/")
        structures = "/api/v1/data-structure-versions/"
        source_structure = self.api("get", f"{structures}{source['input_structure_version']}/")
        structure = self.api("post", structures, {
            "name": "M6-A01 exact author input", "description": "Isolated direction structure",
            "nodes": source_structure["nodes"],
        }, status=201)
        structure_url = f"{structures}{structure['id']}/"
        schema = self.api("get", structure_url + "schema-preview/")
        self.assertTrue(schema["valid"], schema["errors"])
        structure = self.api("post", structure_url + "publish/", {})
        self.assertEqual(structure["status"], "published")
        self.assertNotEqual(structure["id"], source_structure["id"])
        self.assertEqual(structure["nodes"], source_structure["nodes"])
        draft = self.api("post", profiles, {
            "profile_key": "m6-a01-production", "name": "M6 multilingual author combination",
            "model_family": source["model_family"], "operation": source["operation"], "mode": source["mode"],
            "source_projection_key": "direct_v1", "source_projection_config": {},
            "channel_limits": source["channel_limits"],
        }, status=201)
        url = f"{profiles}{draft['id']}/"
        self.api("patch", url, {"source_projection_key": "declarative_v1",
            "source_projection_config": source["source_projection_config"],
            "input_structure_version": structure["id"]})
        rule = PromptProfileFieldRule.objects.get(profile_version=self.prompt_profile)
        source_rule = self.api("get", f"/api/v1/prompt-profile-field-rules/{rule.pk}/")
        writable = (
            "field_definition", "field_definition_version", "source_selector", "channel", "source_version",
            "source_path", "projection_primitive", "position", "label_style", "separator", "required",
            "fixed_value", "has_default", "default_value", "omit_if_empty", "max_chars", "generation_mode",
            "generation_source_selector", "generation_instruction", "generation_output_semantics",
            "generation_required", "language_policy", "length_unit", "generation_min", "generation_max",
            "generation_validator_keys", "generation_context_selectors",
        )
        self.authored_rule = self.api("post", "/api/v1/prompt-profile-field-rules/", {
            **{field: source_rule[field] for field in writable}, "profile_version": draft["id"],
        }, status=201)
        self.assertEqual(self.authored_rule["field_definition_version"], source_rule["field_definition_version"])
        self.assertEqual(self.authored_rule["source_path"], ["direction"])
        self.assertEqual(self.authored_rule["language_policy"], "inherit")
        self.api("patch", url, {"review_state": "ready"})
        profile = self.api("post", url + "publish/", {})
        self.assertEqual((profile["status"], profile["input_structure_version"]), ("published", structure["id"]))
        self.prompt_profile = PromptProfileVersion.objects.get(pk=profile["id"])
        leaf, leaf_contract = self.author_target("m6-a01-production-leaf", self.leaf)
        self.assertEqual(leaf["operation"], "prompt.fields.generate")
        self.assertEqual(leaf["configuration"]["auxiliaries"], [])
        self.assertEqual(leaf_contract["definition"]["interface"]["output"]["adapter"], "text.prompt_fields/v1")
        main, contract = self.author_target("m6-a01-production-main", self.version,
            prompt_profile_version_id=profile["id"],
            auxiliaries=[{"purpose": "generate_fields", "target_version_id": leaf["id"]}])
        self.version = ModelExecutionInstanceVersion.objects.get(pk=main["id"])
        self.definition = json_contract(contract["definition"])
        self.assertEqual(self.definition.sha256, contract["sha256"])
        self.assertEqual(main["configuration"]["inputs"], self.definition.value["configuration"]["inputs"])
        self.assertEqual(self.definition.value["interface"]["prompt"]["profile_id"], profile["id"])
        self.assertEqual(self.definition.value["interface"]["prompt"]["profile_sha256"], self.prompt_profile.contract_sha256)
        self.authored = {"structure_id": structure["id"], "structure_nodes": structure["nodes"],
            "structure_schema": schema["json_schema"], "profile_id": profile["id"],
            "profile_sha256": self.prompt_profile.contract_sha256, "rule_id": self.authored_rule["id"],
            "field_definition_version_id": source_rule["field_definition_version"],
            "leaf_id": leaf["id"], "leaf_definition_sha256": leaf_contract["sha256"],
            "main_id": main["id"], "definition_sha256": contract["sha256"]}

    def approve(self):
        """Hook the reused Run sequence: choose pending uses before review."""
        self.shot_references_url = f"/api/v1/production-runs/{self.run.pk}/manual-shot-references/"
        with CaptureQueriesContext(connection) as queries:
            baseline = self.api("get", self.shot_references_url, {"shot_id": str(self.shot.pk)})
        self.assert_no_writes(queries)
        self.assertTrue(baseline["editability"]["allowed"], baseline["editability"]["reason"])
        self.assertEqual([row["source_mode"] for row in baseline["state"]["uses"]], ["", ""])
        self.assertEqual([row["reference_version_id"] for row in baseline["state"]["uses"]], [None, None])
        self.shot_selection_body = {**deepcopy(self.body), "action": "select", "stage_key": "storyboard_review",
            "unit_key": f"shot:{self.shot.pk}", "scope": {"kind": "shot", "id": str(self.shot.pk)},
            "idempotency_key": "m6-a01-exact-shot-selection",
            "expected_selection_revision": baseline["storyboard_adoption_revision"], "payload": {
                "expected_storyboard_adoption_sha256": baseline["storyboard_adoption_sha256"],
                "expected_uses_sha256": baseline["uses_sha256"], "choices": [{
                    "use_id": str(use.pk), "source_mode": "existing", "reference_version_id": str(version.pk),
                    "asset_id": None, "asset_sha256": version.asset.checksum,
                } for use, version in self.selected]}}
        self.shot_selection_receipt = self.api("post", self.url, self.shot_selection_body, status=201)
        self.assertEqual(self.shot_selection_receipt["result_kind"], "shot_reference_choices")
        self.assertEqual(str(self.shot_selection_receipt["target_id"]), str(self.shot.pk))
        self.assertEqual(self.bible_rows(), self.original_bible)
        commands = list(ProductionManualCommand.objects.order_by("pk").values())
        use_rows = list(ShotReferenceUse.objects.filter(shot=self.shot).order_by("position").values())
        with patch("studio.services.production_manual_shot_references.apply_shot_reference_choices",
                side_effect=AssertionError("Shot command replay must not reselect")) as choose, \
                CaptureQueriesContext(connection) as queries:
            self.selected_state = self.api("get", self.shot_references_url, {"shot_id": str(self.shot.pk)})
            self.assertEqual(self.api("post", self.url, self.shot_selection_body), self.shot_selection_receipt)
        choose.assert_not_called()
        self.assert_no_writes(queries)
        self.assertEqual(list(ProductionManualCommand.objects.order_by("pk").values()), commands)
        self.assertEqual(list(ShotReferenceUse.objects.filter(shot=self.shot).order_by("position").values()), use_rows)
        self.assertNotEqual(baseline["uses_sha256"], self.selected_state["uses_sha256"])
        self.expected_uses = resolve_shot_reference_sources(actor=self.user, project_id=self.project_row.pk,
            shot_ids=[str(self.shot.pk)]).value["shots"][0]["uses"]
        sources, excluded = expand_reviewed_reference_sources(specs=[self.spec],
            sources={"references": mapping("images")}, records=reference_use_records(self.expected_uses), shot_id=str(self.shot.pk))
        self.assertEqual(excluded, [])
        self.body["payload"]["inputs"]["references"]["selection"]["items"] = sources["references"]["items"]
        combinations.M6ProductionCombinationAcceptanceTests.approve(self)

    def confirm_and_freeze(self, prep):
        """Observe the real HTTP preview before the original internal freeze."""
        compiler = combinations.compile_prepared_execution_transport
        previews = []

        def compile_with_http_preview(**kwargs):
            preview = compiler(**kwargs)
            prep.refresh_from_db()
            self.assertIsNone(prep.request_frozen_at)
            field, = prep.field_values.all()
            selection, = prep.field_selections.all()
            self.assertEqual(field.status, "confirmed")
            self.assertEqual(selection.field_value_id, field.pk)
            self.assertEqual(field.field_key, self.authored_rule["field_key"])
            with CaptureQueriesContext(connection) as queries:
                shown = self.api("get", f"/api/v1/execution-preparations/{prep.pk}/prompt-display/",
                    {"expected_preparation_revision": prep.revision})
            self.assert_no_writes(queries)
            self.assertEqual(shown["mode"], "preview")
            self.assertEqual(next(row["text"] for row in shown["channels"] if row["key"] == "positive_prompt"),
                preview.transport.value["prompt_channels"]["values"]["positive_prompt"])
            previews.append(shown)
            return preview

        with patch.object(combinations, "compile_prepared_execution_transport", side_effect=compile_with_http_preview) as compile_preview:
            result = combinations.M6ProductionCombinationAcceptanceTests.confirm_and_freeze(self, prep)
        compile_preview.assert_called_once()
        result["http_preview"], = previews
        self.assertEqual(self.bible_rows(), self.original_bible)
        return result

    def assert_identities_and_ports(self, result, expected):
        refs = result["refs"]
        self.assertEqual(len(refs), 2)
        self.assertEqual([row["shot_reference"] for row in refs], expected)
        self.assertEqual(len({row["entity_id"] for row in refs}), 2)
        self.assertEqual(len({row["item_key"] for row in refs}), 2)
        self.assertEqual(len({row["shot_reference"]["use_id"] for row in refs}), 2)
        self.assertEqual(len({bible_reference_name_key(row["entity_label"]) for row in refs}), 2)
        self.assertTrue(all(row["name_disambiguated"] for row in refs))
        self.assertEqual(len({row["token"] for row in result["bindings"]}), 2)
        graph = result["transport"]["payload"]["compiled_workflow"]["graph"]
        logical = result["facts"]["inputs"]["logical_values"]["references"]
        for index, (row, source) in enumerate(zip(refs, logical)):
            entity = next(entity for entity in self.entities if str(entity.pk) == row["entity_id"])
            use, version = next(pair for pair in self.selected if str(pair[0].pk) == row["shot_reference"]["use_id"])
            self.assertEqual(source["subject"]["name"], entity.name)
            self.assertEqual(source["subject"]["description"], entity.description)
            self.assertEqual(row["original_name"], entity.name)
            self.assertEqual(row["entity_label"], entity.name + " [" + row["item_key"] + "]")
            self.assertEqual(row["shot_reference"]["use_key"], use.use_key)
            self.assertEqual(row["shot_reference"]["purpose"], use.purpose)
            self.assertEqual(row["shot_reference"]["source"]["reference_version_id"], str(version.pk))
            self.assertEqual(row["shot_reference"]["setting"]["setting_revision_id"], str(use.setting_revision_id))
            target = self.definition.value["interface"]["input_targets"][row["target_key"]]
            self.assertEqual(row["target_port_ref"]["input_name"], target["wiring"]["input_names"][index])
            self.assertEqual(row["input_label"], f"<Picture {index + 1}>")
            binding = next(binding for binding in result["bindings"] if binding["item_key"] == row["item_key"])
            self.assertEqual(binding["ports"], [{"target_key": row["target_key"], "port": row["target_port_ref"]}])
            self.assertEqual(graph[f"cr_images_{index}"]["inputs"]["image"], binding["token"])
            self.assertIn(row["entity_label"], result["prompt"])
            self.assertNotIn(entity.description, result["prompt"])
            self.assertEqual(result["display"][row["item_key"]]["original_name"], entity.name)
            self.assertEqual(result["display"][row["item_key"]]["reference_name"], row["entity_label"])
        self.assertEqual(result["facts"]["profile_sources"]["target_language"], "English")
        request, = result["facts"]["field_requests"][0]["value"]["items"]
        self.assertEqual((request["source_language"], request["target_language"]), ("English", "English"))
        self.assertEqual(self.bible_rows(), self.original_bible)

    def test_a01_multilingual_homonyms_use_formal_authors_shot_selection_preview_freeze_and_readonly_replay(self):
        names = ["Ｃafé", "Cafe\u0301"]  # Original A01 Unicode collision, two actual subjects.
        descriptions = ["甲角色的原始描述，保留法文名Ｃafé。", "乙角色的原始描述，保留分解重音Cafe\u0301。"]
        self.assertEqual(bible_reference_name_key(names[0]), bible_reference_name_key(names[1]))
        self.entities = [CharacterBible.objects.create(project=self.project_row, name=name,
            description=description, slug=f"m6-a01-entity-{index}", stable_speaker_id=f"S{index + 1}")
            for index, (name, description) in enumerate(zip(names, descriptions))]
        self.original_bible = self.bible_rows()
        settings_rows = [self.setting(entity, f"原名主图{index}") for index, entity in enumerate(self.entities)]
        self.selected = [self.reference_use(f"角色{index}-独立使用", setting, index)
            for index, setting in enumerate(settings_rows)]
        # These are pending declaration fixtures. ONLY the formal Shot command
        # below establishes their exact source selections used by generation.
        for index, (use, _) in enumerate(self.selected):
            use.purpose = f"第{index + 1}个同名角色的独立创作身份"
            use.source_mode, use.reference_version, use.asset, use.asset_sha256 = "", None, None, ""
            use.save(update_fields=("purpose", "source_mode", "reference_version", "asset", "asset_sha256"))
        unit = self.sources["units"]["value"]["ordered_units"][0]
        unit["subject_keys"] = [["character", str(entity.pk)] for entity in self.entities]
        self.author_exact_chain()
        self.api("patch", f"/api/v1/shots/{self.shot.pk}/", {"character_relations": [
            {"character_id": str(entity.pk), "role": "visible", "position": index}
            for index, entity in enumerate(self.entities)]})
        self.reviewed_production_action("m6-a01")
        self.assertEqual(self.bible_rows(), self.original_bible)
        original_body = deepcopy(self.body)
        prep, receipt = self.create_manual_preparation(original_body)
        original = self.confirm_and_freeze(prep)
        self.assert_identities_and_ports(original, self.expected_uses)
        reordered_body = deepcopy(original_body)
        reordered_body["idempotency_key"] = "m6-a01-explicit-reorder"
        items = reordered_body["payload"]["inputs"]["references"]["selection"]["items"]
        items.reverse()
        for index, item in enumerate(items):
            item["order"] = index
        reordered_prep, reordered_receipt = self.create_manual_preparation(reordered_body)
        reordered = self.confirm_and_freeze(reordered_prep)
        self.assert_identities_and_ports(reordered, self.expected_uses[::-1])
        self.assertNotEqual(prep.pk, reordered_prep.pk)
        self.assertNotEqual(prep.intent_sha256, reordered_prep.intent_sha256)
        self.assertNotEqual(original["bytes"], reordered["bytes"])
        for index, row in enumerate(reordered["refs"]):
            prior = original["refs"][1 - index]
            self.assertEqual((row["item_key"], row["entity_id"], row["entity_label"], row["shot_reference"]),
                (prior["item_key"], prior["entity_id"], prior["entity_label"], prior["shot_reference"]))
            self.assertNotEqual(row["target_port_ref"], prior["target_port_ref"])
            old_binding = next(binding for binding in original["bindings"] if binding["item_key"] == row["item_key"])
            new_binding = next(binding for binding in reordered["bindings"] if binding["item_key"] == row["item_key"])
            self.assertNotEqual(old_binding["ports"], new_binding["ports"])
        self.assert_frozen_reads_and_replay(original, original_body, receipt)
        self.assert_frozen_reads_and_replay(reordered, reordered_body, reordered_receipt)
        # Drift is confined to these test declarations, never to the Bible.
        for use, _ in self.selected:
            use.purpose = "测试后续用途不能覆盖冻结身份 " + use.use_key
            use.save(update_fields=("purpose",))
        commands = list(ProductionManualCommand.objects.order_by("pk").values())
        uses = list(ShotReferenceUse.objects.filter(shot=self.shot).order_by("position").values())
        with patch("studio.services.production_manual_shot_references.apply_shot_reference_choices",
                side_effect=AssertionError("Old Shot command cannot overwrite later choices")) as choose, \
                CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.api("post", self.url, self.shot_selection_body), self.shot_selection_receipt)
        choose.assert_not_called()
        self.assert_no_writes(queries)
        self.assertEqual(list(ProductionManualCommand.objects.order_by("pk").values()), commands)
        self.assertEqual(list(ShotReferenceUse.objects.filter(shot=self.shot).order_by("position").values()), uses)
        self.assert_frozen_reads_and_replay(original, original_body, receipt)
        self.assert_frozen_reads_and_replay(reordered, reordered_body, reordered_receipt)
        self.assertEqual(self.bible_rows(), self.original_bible)
        self.assertEqual(set(self.call_counts().values()), {0})
        for guard in self.guards.values():
            guard.assert_not_called()
        for result in (original, reordered):
            with default_storage.open(result["preparation"].contract.prepared_request_key, "rb") as handle:
                self.assertEqual(handle.read(), result["bytes"])

        def evidence(result):
            return {"preparation_id": str(result["preparation"].pk),
                "intent_sha256": result["preparation"].intent_sha256,
                "request_sha256": sha256(result["bytes"]).hexdigest(), "request_size_bytes": len(result["bytes"]),
                "resolved_inputs_sha256": json_contract(result["facts"]).sha256,
                "http_preview": result["http_preview"], "full_prompt": result["prompt"],
                "references": result["refs"], "bindings": result["bindings"], "display": result["display"]}

        print("M6_A01_AUTHORING_EVIDENCE=" + json.dumps({
            "case": "342-M6-A01", "database": "SQLite in-memory", "temporary_managed_media_root": str(self.media_root),
            "authored": self.authored, "run_id": str(self.run.pk), "shot_id": str(self.shot.pk),
            "bible_originals": [{key: str(value) for key, value in row.items()} for row in self.original_bible],
            "shot_selection_request": self.shot_selection_body, "shot_selection_receipt": self.shot_selection_receipt,
            "selected_shot_state": self.selected_state, "field_receipts": self.field_receipts,
            "original": evidence(original), "reordered": evidence(reordered), "api_trace": self.api_trace,
            "guard_calls": {target: guard.call_count for target, guard in self.guards.items()}, "execution_rows": self.call_counts(),
            "read_replay_sql_writes": 0, "freeze": "ORIGINAL INTERNAL finalize_execution; isolated admission observations; not pure HTTP/live admission/Smoke",
        }, ensure_ascii=False))
