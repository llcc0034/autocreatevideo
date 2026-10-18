"""Bounded M6-09 A02/A03 authoring -> reviewed Shot -> frozen combinations.

Only SQLite test memory and temporary managed media. Resources/Smoke admission
are declared isolated fixtures, never live evidence. Profile, leaf, main target,
upload, storyboard decisions, generation intent and field selection use HTTP.
Final request freeze uses the ORIGINAL INTERNAL finalize_execution writer with
isolated admission observations: this is explicitly NOT a pure HTTP freeze.
There is no main/auxiliary dispatch, provider response or generated result.
"""
from copy import deepcopy
from hashlib import sha256
import json
from unittest.mock import Mock, patch

from django.core.files.storage import default_storage
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import (
    Asset, AssetGenerationJob, AssetReferenceSeries, CharacterBible,
    ExecutionPreparation, GenerationAttempt, GenerationTask,
    ModelDeploymentSmokeAttempt, ModelExecutionInstanceVersion,
    PromptProfileFieldRule, PromptProfileVersion, ProductionManualCommand,
    ProductionRun, ShotReferenceUse, WorkflowSmokeAttempt,
)
from studio.services.asset_provenance import ASSET_UPLOAD_SOURCE_INFO
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.execution_reference_display import frozen_reference_display
from studio.services.execution_workflow_uses import read_execution_workflow_uses
from studio.services.json_contracts import canonical_json_bytes, json_contract
from studio.services.production_reference_mapping import (
    expand_reviewed_reference_sources, reference_use_records, reviewed_reference_item_key,
)
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.shot_reference_sources import resolve_shot_reference_sources
from studio.tests import test_m6_authoring_waiting as authoring
from studio.tests import test_m6_mixed_acceptance as mixed
from studio.tests import test_m6_reference_acceptance as references
from studio.tests import test_manual_generation_shot_sources as manual_sources
from studio.tests import test_production_manual_bible as manual_bible
from studio.tests import test_production_manual_media as manual_media
from studio.tests.test_asset_management_api import image_upload
from studio.tests.test_execution_production_auxiliaries import production_observations
from studio.tests.test_production_reference_mapping import mapping


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class M6ProductionCombinationAcceptanceTests(TestCase):
    # Borrow helpers, not TestCase inheritance (exactly two substantive tests).
    _create_version = references.M6ReferenceAcceptanceTests._create_version
    selection = references.M6ReferenceAcceptanceTests.selection
    configure = references.M6ReferenceAcceptanceTests.configure
    setting = references.M6ReferenceAcceptanceTests.setting
    reference_use = references.M6ReferenceAcceptanceTests.reference_use
    api = authoring.M6AuthoringWaitingTests.api
    author_target = authoring.M6AuthoringWaitingTests.author_target
    post = manual_media.ManualMediaPreparationTests.post
    approve = manual_sources.ManualGenerationShotSourcesTests.approve
    read = manual_bible.ProductionManualBibleTests.read
    adopt_source = manual_bible.ProductionManualBibleTests.adopt_source
    approval = manual_bible.ProductionManualBibleTests.approval

    def setUp(self):
        self.assertEqual(connection.vendor, "sqlite")
        self.assertIn("memory", str(connection.settings_dict["NAME"]))
        self.guards = {}
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection", "socket.getaddrinfo",
            "requests.sessions.Session.request", "studio.services.provider_registry._request_json",
            "studio.services.provider_registry.resolve_credential", "studio.services.comfyui.ComfyUIClient",
            "studio.services.execution_advancement.dispatch_asset_execution",
            "studio.services.execution_asset_dispatch.dispatch_asset_execution",
            "studio.services.execution_production_dispatch.dispatch_production_execution",
            "studio.services.execution_asset_auxiliaries.create_asset_field_child",
            "studio.services.execution_production_auxiliaries.create_production_field_child",
        ):
            self.guards[target] = self.enterContext(patch(target, side_effect=AssertionError(
                "M6 isolated acceptance forbids network, provider and main/auxiliary dispatch")))

    def tearDown(self):
        for guard in self.guards.values():
            guard.assert_not_called()

    @staticmethod
    def call_counts():
        return {model.__name__: model.objects.count() for model in (
            GenerationAttempt, GenerationTask, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt,
        )}

    def author_chain(self, key, **target_changes):
        """Reuse the formal author helpers with this combination's real inputs."""
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        profiles = "/api/v1/prompt-profile-versions/"
        source = self.api("get", f"{profiles}{self.prompt_profile.pk}/")
        draft = self.api("post", profiles, {
            "profile_key": key, "name": "Isolated M6 production combination " + key,
            "model_family": source["model_family"], "operation": source["operation"], "mode": source["mode"],
            "source_projection_key": "direct_v1", "source_projection_config": {},
            "channel_limits": source["channel_limits"],
        }, status=201)
        url = f"{profiles}{draft['id']}/"
        self.api("patch", url, {"source_projection_key": "declarative_v1",
            "source_projection_config": source["source_projection_config"],
            "input_structure_version": source["input_structure_version"]})
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
        self.api("patch", url, {"review_state": "ready"})
        profile = self.api("post", url + "publish/", {})
        self.assertEqual(profile["status"], "published")
        self.prompt_profile = PromptProfileVersion.objects.get(pk=profile["id"])
        leaf, leaf_contract = self.author_target(key + "-leaf", self.leaf)
        self.assertEqual(leaf["operation"], "prompt.fields.generate")
        self.assertEqual(leaf["configuration"]["auxiliaries"], [])
        self.assertEqual(leaf_contract["definition"]["interface"]["output"]["adapter"], "text.prompt_fields/v1")
        main, contract = self.author_target(key + "-main", self.version,
            prompt_profile_version_id=profile["id"],
            auxiliaries=[{"purpose": "generate_fields", "target_version_id": leaf["id"]}], **target_changes)
        self.version = ModelExecutionInstanceVersion.objects.get(pk=main["id"])
        self.definition = json_contract(contract["definition"])
        self.assertEqual(self.definition.sha256, contract["sha256"])
        self.assertEqual(main["configuration"]["inputs"], self.definition.value["configuration"]["inputs"])
        self.authored = {"profile_id": profile["id"], "rule_id": self.authored_rule["id"],
            "leaf_id": leaf["id"], "main_id": main["id"], "definition_sha256": contract["sha256"]}

    def reviewed_production_action(self, key):
        """Manual-media fixture's formal Run/target/adoption/review sequence."""
        self.project = self.project_row
        catalog = production_stage_catalog()
        created = self.api("post", "/api/v1/production-runs/", {
            "project_id": str(self.project.pk), "plan_source": "manual", "idempotency_key": key + "-run",
            "allow_external_calls": False, "config_overrides": {}, "manual_plan": {
                "stage_catalog_key": catalog.value["key"], "expected_stage_catalog_sha256": catalog.sha256,
                "scope": {"kind": "project"}, "delivery": {"kind": "episode_masters"}, "initial_content": None}}, status=201)
        self.run = ProductionRun.objects.get(pk=created["id"])
        base = f"/api/v1/production-runs/{self.run.pk}/"
        self.url = base + "manual-actions/"
        targets = self.api("get", base + "targets/", {"kind": "shot"})
        target, = targets["results"]
        self.assertEqual(target["scope"], {"kind": "shot", "id": str(self.shot.pk)})
        self.body = {"action": "generate", "stage_key": "media_generation", "unit_key": target["unit_key"],
            "scope": target["scope"], "expected_run_plan_sha256": targets["run_plan_sha256"],
            "expected_selection_revision": None, "idempotency_key": key + "-original", "payload": {
                "execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
                "inputs": deepcopy(self.sources), "parameters": self.parameter_values, "manual_channels": {},
                "subject_facts": [], "source_language": "English", "target_language": "English"}}
        if ShotReferenceUse.objects.filter(shot=self.shot, setting_revision__isnull=False).exists():
            # The formal Shot reviewer requires the original Bible approval for
            # exact settings. Reuse the confirmed-script/Bible API fixtures;
            # do not fabricate an ApprovalDecision or relax the prerequisite.
            self.actor = self.user
            revision = manual_bible.confirmed_bible_source(self)
            generation = self.body
            self.body = {**generation, "action": "adopt", "stage_key": "script_input",
                "unit_key": f"project:{self.project.pk}", "scope": {"kind": "project", "id": str(self.project.pk)},
                "expected_selection_revision": 0, "idempotency_key": key + "-script", "payload": {
                    "content": {"kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256},
                    "comment": "Original isolated confirmed-script fixture"}}
            self.api("post", self.url, self.body, status=201)
            self.read_url = base + "bible-review/"
            self.adopt_source()
            self.api("post", self.url, self.approval(), status=201)
            self.assertEqual(self.read()["approval"]["status"], "current")
            self.body = generation
        detail = self.api("get", base + "manual-storyboard/")
        self.api("post", self.url, {**deepcopy(self.body), "action": "adopt", "stage_key": "storyboard",
            "unit_key": f"project:{self.project.pk}", "scope": {"kind": "project", "id": str(self.project.pk)},
            "idempotency_key": key + "-adopt", "expected_selection_revision": detail["adoption"]["revision"],
            "payload": {"source": {"kind": "existing_storyboard"}, "expected_content_sha256": detail["content_sha256"],
                "reviewed_shot_ids": [str(self.shot.pk)], "comment": "Isolated authored combination fixture"}}, status=201)
        self.review_url = base + "manual-storyboard-review/"
        self.approve()  # Reused formal reviewer, not a fabricated approval row.
        self.before_calls = self.call_counts()
        self.assertEqual(set(self.before_calls.values()), {0})
        self.before_jobs = list(AssetGenerationJob.objects.values())

    def create_manual_preparation(self, body):
        receipt = self.api("post", self.url, body, status=202)
        prep = ExecutionPreparation.objects.get(pk=receipt["target_id"])
        self.assertEqual(prep.stage_run_id, self.run.stages.get(stage_key="media_generation").pk)
        self.assertEqual(prep.purpose, "production")
        self.assertIsNone(prep.asset_job_id)
        self.assertEqual(prep.unit_key, f"shot:{self.shot.pk}")
        self.assertEqual(prep.status, "preparing")
        self.assertIsNone(prep.next_check_at)
        self.assertIsNone(prep.request_frozen_at)
        # Manual creation stops before advancement. Unlike the asset-job API,
        # it does not evaluate fields automatically; use the original isolated
        # field evaluator before contributing/confirming the required value.
        facts = deepcopy(prep.contract.resolved_inputs)
        prep = evaluate_preparation_fields(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.user)
        self.assertEqual((prep.status, prep.reason_code), ("awaiting_input", "execution_fields_missing"))
        self.assertIsNone(prep.next_check_at)
        detail = prep.contract
        self.assertEqual(detail.resolved_inputs, facts)
        self.assertEqual(detail.intent["request"]["requested"]["expected_storyboard_review"],
            body["payload"]["expected_storyboard_review"])
        self.assertEqual(detail.resolved_inputs["production_unit"], {
            "schema_version": 1, "run_plan_sha256": body["expected_run_plan_sha256"], "scope": body["scope"],
            "project_id": str(self.project.pk), "episode_id": str(self.shot.episode_id)})
        return prep, receipt

    def confirm_and_freeze(self, prep):
        """HTTP field contribution/selection; internal original writer freezes."""
        facts = deepcopy(prep.contract.resolved_inputs)
        batch, = facts["field_requests"]
        request, = batch["value"]["items"]
        self.assertEqual(request["field_contract"]["field_key"], self.authored_rule["field_key"])
        text = "Café turns slowly. Preserve each supplied reference identity and its explicitly declared use."
        saved = self.api("post", "/api/v1/execution-field-values/", {
            "preparation_id": str(prep.pk), "unit_key": request["unit_key"],
            "field_key": self.authored_rule["field_key"], "text": text, "revision": 1,
            "expected_preparation_revision": prep.revision}, status=201)
        field = saved["field"]
        self.assertEqual(field["status"], "pending")
        self.assertIsNone(saved["selection"])
        prep.refresh_from_db()
        self.api("post", f"/api/v1/execution-field-values/{field['id']}/confirm/", {
            "expected_text_sha256": field["text_sha256"], "expected_source_sha256": field["source_sha256"],
            "expected_preparation_revision": prep.revision, "select_for_execution": True})
        prep.refresh_from_db()
        prep = evaluate_preparation_fields(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.user)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.user)
        fulltext = preview.transport.value["prompt_channels"]["values"]["positive_prompt"]
        self.assertEqual(fulltext.count(text), 1)
        self.assertNotIn("NOT_IMPLICIT_", fulltext)
        self.assertIsNone(prep.request_frozen_at)
        basis_revision = prep.revision
        collector = Mock(side_effect=production_observations)
        prep = finalize_execution(preparation_id=prep.pk, expected_preparation_revision=basis_revision,
            actor=self.user, collect_admission_facts=collector)
        self.assertEqual(prep.status, "ready", prep.reason)
        collector.assert_called_once()
        self.assertIsNotNone(prep.request_frozen_at)
        detail, definition, transport = read_frozen_execution_request(prep)
        self.assertEqual(transport, preview.transport)
        document = deepcopy(detail.prepared_request)
        with default_storage.open(detail.prepared_request_key, "rb") as handle:
            content = handle.read()
        self.assertEqual(content, canonical_json_bytes(document))
        self.assertEqual(sha256(content).hexdigest(), prep.request_sha256)
        self.assertEqual(len(content), detail.prepared_request_size_bytes)
        self.assertEqual(detail.resolved_inputs, facts)
        uses = read_execution_workflow_uses(definition, json_contract(facts["inputs"]))
        display = frozen_reference_display(facts, uses)
        refs = request["reference_facts"]["logical_inputs"]
        flattened = [part for row in refs for part in [row, *([row["audio_use"]] if "audio_use" in row else [])]]
        bindings = transport.value["payload"]["media_preparation"]["bindings"]
        self.assertEqual({row["item_key"] for row in bindings}, set(display))
        for row in flattened:
            self.assertIn(row["entity_label"], fulltext)
            self.assertIn(row["input_label"], fulltext)
            self.assertEqual(display[row["item_key"]]["shot_reference"], row["shot_reference"])
            asset = Asset.objects.get(pk=row["asset_id"])
            with asset.file.open("rb") as handle:
                actual = handle.read()
            binding = next(value for value in bindings if value["item_key"] == row["item_key"])
            self.assertEqual(sha256(actual).hexdigest(), binding["media_facts"]["sha256"])
            self.assertEqual(asset.checksum, binding["media_facts"]["sha256"])
            self.assertEqual(row["shot_reference"]["asset"]["sha256"], asset.checksum)
        return {"preparation": prep, "receipt": None, "basis_revision": basis_revision,
            "facts": facts, "refs": refs, "flattened": flattened, "transport": deepcopy(transport.value),
            "document": document, "bytes": content, "collector": collector, "display": display,
            "prompt": fulltext, "bindings": bindings}

    def assert_frozen_reads_and_replay(self, original, body, receipt):
        """No selection/compilation/admission/storage writes during frozen reads."""
        prep = original["preparation"]
        prep.refresh_from_db()
        before = list(ExecutionPreparation.objects.filter(pk=prep.pk).values())
        commands = list(ProductionManualCommand.objects.values())
        base = f"/api/v1/execution-preparations/{prep.pk}/"
        with patch("studio.services.shot_reference_sources.resolve_shot_reference_sources",
                side_effect=AssertionError("Frozen read/replay must not reread current Shot choices")) as resolve, \
                patch("studio.services.execution_finalization.compile_prepared_execution_transport",
                    side_effect=AssertionError("Frozen replay must not recompile")) as compile_request, \
                patch("studio.services.execution_finalization.store_prepared_request",
                    side_effect=AssertionError("Frozen replay must not rewrite bytes")) as store, \
                CaptureQueriesContext(connection) as queries:
            for _ in range(2):
                shown = self.api("get", base)
                self.assertEqual(shown["resolved_inputs"], original["facts"])
                self.assertEqual(shown["prepared_request"], original["document"])
                prompt = self.api("get", base + "prompt-display/", {"expected_preparation_revision": prep.revision})
                self.assertEqual(prompt["mode"], "frozen")
                self.assertEqual(next(row["text"] for row in prompt["channels"] if row["key"] == "positive_prompt"), original["prompt"])
                self.assertEqual(prompt["sources"]["references"], [{"label": row["input_label"],
                    "name": row["entity_label"], "category": row["category"], "guidance": ""}
                    for row in original["flattened"]])
                metrics = self.api("get", base + "storage-metrics/", {"expected_preparation_revision": prep.revision})
                sizes = {row["key"]: row for row in metrics["items"]}
                self.assertEqual((sizes["resolved_inputs"]["size_bytes"], sizes["resolved_inputs"]["sha256"]),
                    (json_contract(original["facts"]).size_bytes, json_contract(original["facts"]).sha256))
                self.assertEqual((sizes["prepared_request"]["state"], sizes["prepared_request"]["size_bytes"],
                    sizes["prepared_request"]["sha256"]), ("recorded", len(original["bytes"]), sha256(original["bytes"]).hexdigest()))
                detail, definition, transport = read_frozen_execution_request(ExecutionPreparation.objects.get(pk=prep.pk))
                self.assertEqual(transport.value, original["transport"])
                self.assertEqual(transport.value["payload"]["media_preparation"]["bindings"], original["bindings"])
                uses = read_execution_workflow_uses(definition, json_contract(detail.resolved_inputs["inputs"]))
                self.assertEqual(frozen_reference_display(detail.resolved_inputs, uses), original["display"])
                with default_storage.open(detail.prepared_request_key, "rb") as handle:
                    self.assertEqual(handle.read(), original["bytes"])
            replay = self.api("post", self.url, body)
            self.assertEqual(replay, receipt)
            replayed = finalize_execution(preparation_id=prep.pk, expected_preparation_revision=original["basis_revision"],
                actor=self.user, collect_admission_facts=original["collector"])
            self.assertEqual(replayed.request_sha256, sha256(original["bytes"]).hexdigest())
        resolve.assert_not_called()
        compile_request.assert_not_called()
        store.assert_not_called()
        original["collector"].assert_called_once()
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))
        self.assertEqual(list(ExecutionPreparation.objects.filter(pk=prep.pk).values()), before)
        self.assertEqual(list(ProductionManualCommand.objects.values()), commands)
        self.assertEqual(list(AssetGenerationJob.objects.values()), self.before_jobs)
        self.assertEqual(self.call_counts(), self.before_calls)
        self.assertEqual(set(self.call_counts().values()), {0})
        self.assertFalse(AssetGenerationJob.objects.filter(parent_preparation__isnull=False).exists())
        self.assertFalse(prep.auxiliary_generation_attempts.exists())

    def evidence(self, case, original, reordered, **extra):
        def summary(row):
            prep = row["preparation"]
            return {"preparation_id": str(prep.pk), "request_key": prep.request_key, "intent_sha256": prep.intent_sha256,
                "request_size_bytes": len(row["bytes"]), "request_sha256": sha256(row["bytes"]).hexdigest(),
                "resolved_inputs_sha256": json_contract(row["facts"]).sha256,
                "references": row["flattened"], "bindings": row["bindings"]}
        print("M6_COMBINATION_EVIDENCE=" + json.dumps({"case": case, "authored": self.authored,
            "run_id": str(self.run.pk), "shot_id": str(self.shot.pk), "original": summary(original),
            "reordered": summary(reordered), "calls": self.call_counts(),
            "freeze": "original internal writer with isolated admission observations; not pure HTTP",
            "media": "temporary managed fixtures; no live Smoke or model result", **extra}, ensure_ascii=False))

    def test_a02_reviewed_shot_uniform_and_raincoat_keep_distinct_png_versions_through_author_and_freeze(self):
        references.M6ReferenceAcceptanceTests.setUp(self)
        entity = CharacterBible.objects.create(project=self.project_row, name="林岚", slug="m6-two-settings", stable_speaker_id="S1")
        primary = self.setting(entity, "主形象")
        parent = AssetReferenceSeries.objects.create(owner=self.user, name="Isolated primary", role="primary", bible_variant=primary.variant)
        settings_rows = [self.setting(entity, name, primary) for name in ("校服", "雨衣")]
        selected = [self.reference_use(key, setting, index, parent)
            for index, (key, setting) in enumerate(zip(("uniform", "raincoat"), settings_rows))]
        self.assertNotEqual(settings_rows[0].variant_id, settings_rows[1].variant_id)
        self.assertEqual({row.primary_setting_revision_id for row in settings_rows}, {primary.pk})
        self.assertEqual(len({asset.checksum for asset in self.assets}), 2)
        self.assertEqual(len({version.pk for _, version in selected}), 2)
        self.assertEqual(len({use.pk for use, _ in selected}), 2)
        unit = self.sources["units"]["value"]["ordered_units"][0]
        unit["subject_keys"] = [["character", str(entity.pk)]]
        expected = resolve_shot_reference_sources(actor=self.user, project_id=self.project_row.pk,
            shot_ids=[str(self.shot.pk)]).value["shots"][0]["uses"]
        sources, excluded = expand_reviewed_reference_sources(specs=[self.spec],
            sources={"references": mapping("images")}, records=reference_use_records(expected), shot_id=str(self.shot.pk))
        self.assertEqual(excluded, [])
        self.sources["references"]["selection"]["items"] = sources["references"]["items"]
        self.author_chain("m6-a02-production")
        self.reviewed_production_action("m6-a02")
        original_body = deepcopy(self.body)
        prep, receipt = self.create_manual_preparation(original_body)
        original = self.confirm_and_freeze(prep)
        self.assertEqual([row["shot_reference"] for row in original["refs"]], expected)
        self.assertEqual({row["entity_id"] for row in original["refs"]}, {str(entity.pk)})
        self.assertEqual(len({row["entity_label"] for row in original["refs"]}), 2)
        for index, (row, setting, (use, version)) in enumerate(zip(original["refs"], settings_rows, selected)):
            self.assertEqual(row["input_label"], f"<Picture {index + 1}>")
            setting_variant_id = str(setting.variant_id)
            self.assertEqual(row["shot_reference"]["setting"], {
                "appearance_id": setting_variant_id, "setting_revision_id": str(setting.pk), "revision": setting.revision,
                "content_sha256": setting.content_sha256, "entity_kind": "character", "entity_id": str(entity.pk),
                "appearance_role": "variant", "reference_name": setting.reference_name})
            self.assertEqual(row["shot_reference"]["use_id"], str(use.pk))
            self.assertEqual(row["shot_reference"]["source"], {"kind": "reference_version",
                "reference_version_id": str(version.pk), "series_id": str(version.series_id),
                "version": version.version, "acquisition_source": "existing"})
            self.assertEqual(original["transport"]["payload"]["compiled_workflow"]["graph"][f"cr_images_{index}"]["inputs"]["image"],
                original["bindings"][index]["token"])
        reordered_body = deepcopy(original_body)
        reordered_body["idempotency_key"] = "m6-a02-explicit-reorder"
        reordered_body["payload"]["inputs"]["references"]["selection"]["items"].reverse()
        for index, item in enumerate(reordered_body["payload"]["inputs"]["references"]["selection"]["items"]):
            item["order"] = index
        new_prep, _ = self.create_manual_preparation(reordered_body)
        reordered = self.confirm_and_freeze(new_prep)
        self.assertNotEqual(prep.pk, new_prep.pk)
        self.assertNotEqual(prep.intent_sha256, new_prep.intent_sha256)
        self.assertEqual([row["shot_reference"] for row in reordered["refs"]], expected[::-1])
        for index, row in enumerate(reordered["refs"]):
            prior = original["refs"][1 - index]
            self.assertEqual((row["item_key"], row["entity_label"]), (prior["item_key"], prior["entity_label"]))
            self.assertEqual(row["input_label"], f"<Picture {index + 1}>")
        entity.name = "Later author name must not rewrite frozen references"
        entity.save(update_fields=("name",))
        self.use.purpose = "Later creative purpose must not replace reviewed facts"
        self.use.save(update_fields=("purpose",))
        self.assert_frozen_reads_and_replay(original, original_body, receipt)
        self.evidence("A02", original, reordered,
            setting_variant_ids=[str(row.variant_id) for row in settings_rows],
            setting_variant_wire_field="shot_reference.setting.appearance_id")

    def test_a03_governed_png_two_videos_same_wav_uses_reorder_as_new_intent_and_replay_original_bytes_ports(self):
        mixed.M6MixedReferenceAcceptanceTests.setUp(self)
        upload = image_upload("m6-governed-reference.png")
        uploaded_bytes = upload.read()
        upload.seek(0)
        response = self.client.post("/api/v1/assets/", {"name": "Governed isolated PNG", "kind": "reference", "file": upload}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        uploaded = Asset.objects.get(pk=response.data["id"])
        self.assertEqual(uploaded.source_info, ASSET_UPLOAD_SOURCE_INFO)
        self.assertEqual(uploaded.checksum, sha256(uploaded_bytes).hexdigest())
        self.use.reference_version = None
        self.use.setting_revision = None
        self.use.asset = uploaded
        self.use.asset_sha256 = uploaded.checksum
        self.use.source_mode = "upload"
        self.use.purpose = "Uploaded picture retained as explicit image reference"
        self.use.save(update_fields=("reference_version", "setting_revision", "asset", "asset_sha256", "source_mode", "purpose"))
        self.sources["references"]["selection"] = {"source": "asset_upload", "items": [{
            "kind": "asset", "item_key": reviewed_reference_item_key(input_key="references", shot_id=str(self.shot.pk), reference_key="shot-use:" + self.use.use_key),
            "asset_id": str(uploaded.pk), "purpose": None, "subject_ref": None,
            "scope_ref": {"kind": "shot", "id": str(self.shot.pk)}, "order": 0, "paired_to": None}]}
        specs = deepcopy(self.definition.value["configuration"]["inputs"])
        next(spec for spec in specs if spec["key"] == "references")["media_source_policy"]["default_source"] = "asset_upload"
        self.author_chain("m6-a03-production", inputs=specs)
        self.reviewed_production_action("m6-a03")
        original_body = deepcopy(self.body)
        prep, receipt = self.create_manual_preparation(original_body)
        original = self.confirm_and_freeze(prep)
        self.assertEqual(len(original["flattened"]), 5)
        self.assertEqual({row["input_label"] for row in original["flattened"]},
            {"<Picture 1>", "<Video 1>", "<Video 2>", "<Audio 1>", "<Audio 2>"})
        picture = original["refs"][0]
        self.assertEqual(picture["asset_id"], str(uploaded.pk))
        self.assertEqual(picture["source_selection"], {"source": "asset_upload", "overridden": False})
        self.assertEqual(picture["shot_reference"]["source_mode"], "upload")
        self.assertEqual(picture["shot_reference"]["source"], {"kind": "asset", "asset_id": str(uploaded.pk)})
        logical = original["facts"]["inputs"]["logical_values"]
        clips = logical["clips"]
        paired, = logical["paired"]
        ambient, = logical["ambient"]
        self.assertEqual(len({row["asset_id"] for row in clips}), 2)
        self.assertEqual(len({row["media_facts"]["sha256"] for row in clips}), 2)
        self.assertTrue(all(row["shot_reference"]["source_mode"] == "existing" for row in clips))
        self.assertEqual(paired["asset_id"], ambient["asset_id"])
        self.assertEqual(paired["media_facts"]["mime_type"], "audio/wav")
        self.assertEqual(paired["media_facts"]["sha256"], ambient["media_facts"]["sha256"])
        self.assertNotEqual(paired["item_key"], ambient["item_key"])
        self.assertNotEqual(paired["shot_reference"]["use_id"], ambient["shot_reference"]["use_id"])
        self.assertNotEqual(paired["shot_reference"]["purpose"], ambient["shot_reference"]["purpose"])
        self.assertEqual((paired["purpose"], ambient["purpose"]), ("audio_reference", "audio_reference"))
        self.assertEqual(paired["paired_to"], clips[1]["item_key"])
        self.assertIsNone(ambient["paired_to"])
        audio_parent = next(row for row in original["refs"] if "audio_use" in row)
        self.assertEqual(audio_parent["item_key"], clips[1]["item_key"])
        self.assertEqual(audio_parent["audio_use"]["item_key"], paired["item_key"])
        self.assertEqual(audio_parent["input_label"], "<Video 2>")
        by_item = {row["item_key"]: row for row in original["bindings"]}
        self.assertNotEqual(by_item[paired["item_key"]]["token"], by_item[ambient["item_key"]]["token"])
        graph = original["transport"]["payload"]["compiled_workflow"]["graph"]
        for index, row in enumerate(clips):
            self.assertEqual(graph[f"cr_videos_{index}"]["inputs"]["file"], by_item[row["item_key"]]["token"])
        self.assertEqual(graph["cr_paired_audio_1"]["inputs"]["audio"], by_item[paired["item_key"]]["token"])
        self.assertEqual(graph["cr_audios_0"]["inputs"]["audio"], by_item[ambient["item_key"]]["token"])
        reordered_body = deepcopy(original_body)
        reordered_body["idempotency_key"] = "m6-a03-explicit-reorder"
        reordered_body["payload"]["inputs"]["clips"]["selection"]["items"].reverse()
        for index, row in enumerate(reordered_body["payload"]["inputs"]["clips"]["selection"]["items"]):
            row["order"] = index
        new_prep, _ = self.create_manual_preparation(reordered_body)
        reordered = self.confirm_and_freeze(new_prep)
        self.assertNotEqual(prep.pk, new_prep.pk)
        self.assertNotEqual(prep.intent_sha256, new_prep.intent_sha256)
        for row in reordered["flattened"]:
            prior = next(part for part in original["flattened"] if part["item_key"] == row["item_key"])
            self.assertEqual((row["shot_reference"], row["entity_label"]), (prior["shot_reference"], prior["entity_label"]))
            if row["category"] == "video":
                # Source contracts cover position; a NEW order has a new digest,
                # while its exact use/version/file facts remain the same.
                self.assertNotEqual(row["source_sha256"], prior["source_sha256"])
        reordered_paired = reordered["facts"]["inputs"]["logical_values"]["paired"][0]
        self.assertEqual(reordered_paired["paired_to"], paired["paired_to"])
        parent = next(row for row in reordered["refs"] if "audio_use" in row)
        self.assertEqual((parent["item_key"], parent["input_label"]), (clips[1]["item_key"], "<Video 1>"))
        self.assertNotEqual(by_item[paired["item_key"]]["ports"],
            next(row for row in reordered["bindings"] if row["item_key"] == paired["item_key"])["ports"])
        for use in ShotReferenceUse.objects.filter(shot=self.shot):
            use.purpose = "Later purpose cannot replace original frozen use " + use.use_key
            use.save(update_fields=("purpose",))
        for asset in [uploaded, *self.media_by_id.values()]:
            asset.name = "Later library name cannot replace original frozen name"
            asset.save(update_fields=("name",))
        self.assert_frozen_reads_and_replay(original, original_body, receipt)
        # A new business key with the old review is a real stale request, not a
        # replay. It must not create another preparation after choice drift.
        before = ExecutionPreparation.objects.count()
        stale = {**deepcopy(original_body), "idempotency_key": "m6-a03-stale-new-intent"}
        self.api("post", self.url, stale, status=400)
        self.assertEqual(ExecutionPreparation.objects.count(), before)
        self.assertEqual(self.call_counts(), self.before_calls)
        self.evidence("A03", original, reordered, upload_asset_id=str(uploaded.pk),
            upload_bytes_sha256=sha256(uploaded_bytes).hexdigest(), paired_parent_item_key=paired["paired_to"],
            same_wav_asset_id=paired["asset_id"], independent_wav_item_keys=[paired["item_key"], ambient["item_key"]])
