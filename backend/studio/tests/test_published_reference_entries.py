"""Full v21 through three real preparation producers, without remote calls.

The Recipe root is an isolated frozen-plan fixture, not evidence that overall
Recipe publication/admission has passed. Asset and manual roots use public APIs.
Only explicit human field contributions are selected; no model output is faked.
"""
from copy import deepcopy
from uuid import uuid4

from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient

from studio.models import (ApprovalDecision, AssetGenerationJob, Episode, ExecutionPreparation,
    GenerationAttempt, ModelDeployment, ModelExecutionInstanceVersion, ModelProfileVersion,
    ProductionRecipeVersion, ProductionRun, PropBible, ProviderProfile, SeriesProject, Shot, ShotReferenceUse, StageRun)
from studio.services.configuration import resolve_configuration
from studio.services.bible_variants import create_variant
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_draft, create_execution_instance, publish_execution_version, update_execution_draft
from studio.services.execution_preparation_fields import create_manual_execution_field, confirm_execution_field
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_profile_inputs import profile_input_targets
from studio.services.execution_profile_preparation import compile_preparation_profile
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit, resolve_recipe_media_sources
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_runs import _formal_snapshot, _save_stage_snapshot
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.security_boundaries import boundary_int
from studio.tests import test_execution_media_processing as media_fixture
from studio.tests import test_execution_auxiliary_definitions as auxiliary_fixture
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_execution_text_contracts import text_specification
from studio.tests.test_published_reference_profile import publish_full_reference_fixture


def literal(value):
    return {"kind": "literal", "value": deepcopy(value)}


def field(key):
    return {"kind": "context_field", "selector_key": key, "scope": {"kind": "shot"}}


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class PublishedReferenceEntryTests(TestCase):
    # Declare both model and workflow contracts before either is frozen. This
    # fictional target accepts direct existing/uploaded references, with no
    # requirement to generate Bible images in the same Run.
    requires_bible = False
    _create_version = media_fixture.ExecutionMediaProcessingTests._create_version
    compile = media_fixture.ExecutionMediaProcessingTests.compile
    embedded_sources = media_fixture.ExecutionMediaProcessingTests.embedded_sources
    input = staticmethod(auxiliary_fixture.ExecutionAuxiliaryDefinitionTests.input)

    @staticmethod
    def write(row, key, value, **options):
        assign_json_contract(row, key, value, **options)
        row.save()
        return row

    def leaf(self):
        provider = self.write(ProviderProfile(key="parity-text", name="Fields", driver_type="openai_compatible", modalities="text"),
            "driver_config", {"base_url": "https://api.example.com", "chat_path": "/v1/chat/completions", "timeout_seconds": 30, "capability_ttl_seconds": 3600})
        model = self.write(ModelProfileVersion(profile_key="parity-text", version=1, provider=provider,
            model_identifier="explicit-field-model", modality="text", status="published"), "specification", text_specification())
        deployment = self.write(ModelDeployment(key="parity-text", name="Fields", profile_version=model), "runtime_config", {})
        instance = create_execution_instance(key="parity-text", name="Fields", description="", actor=self.user)
        leaf = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.user,
            selection={"deployment": deployment, "operation": "prompt.fields.generate", "mode": "", "workflow_version": None,
                "prompt_profile_version": None, "prompt_mode": "none"})
        leaf = update_execution_draft(version_id=leaf.pk, expected_revision=leaf.revision, actor=self.user,
            changes={"inputs": [self.input("field_request", "field_request", kind="json", target_kind="operation_input")]})
        return publish_execution_version(version_id=leaf.pk, expected_revision=leaf.revision,
            expected_definition_sha256=build_execution_definition(leaf.pk).sha256, actor=self.user)

    def setUp(self):
        media_fixture.ExecutionMediaProcessingTests.setUp(self)
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=("is_active",))
        self.model = self.profile
        payload, self.prompt_profile = publish_full_reference_fixture(self.user)
        specification = deepcopy(self.model.specification)
        specification["model_family"] = payload["model_family"]
        self.write(self.model, "specification", specification)
        self.request_parameters = {key: value for key, value in self.parameters["values"].items()
            if self.parameters["sources"][key] == "request"}
        specs = deepcopy(self.definition.value["configuration"]["inputs"])
        for spec in specs:
            spec["semantic_contract"]["allowed_scope_kinds"] = ["request", "shot"]
        self.profile_keys = {}
        for target in profile_input_targets(self.prompt_profile):
            key = "content" if not target.startswith("context.") else "context_" + str(len(self.profile_keys))
            self.profile_keys[target] = key
            specs.append(self.input(key, target, kind="json", position=len(specs)))
        for spec in specs:
            spec["semantic_contract"]["allowed_scope_kinds"] = ["request", "shot"]
            if spec["key"] == "references":
                spec["semantic_contract"]["allowed_subject_kinds"] = ["prop"]
        version = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.user,
            changes={"prompt_mode": "profile", "prompt_profile_version": self.prompt_profile, "inputs": specs,
                "auxiliaries": [{"purpose": "generate_fields", "target_version_id": str(self.leaf().pk)}]})
        self.definition = build_execution_definition(version.pk)
        self.version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        sources = self.embedded_sources()
        self.project = SeriesProject.objects.create(owner=self.user, title="Full v21 entry comparison", **{**PROJECT_CONFIG, "language": "English"})
        self.episode = Episode.objects.create(project=self.project, number=1, title="One independently generated shot")
        self.shot = Shot.objects.create(episode=self.episode, number=1, title="Parcel", media_type="video", duration_ms=5000,
            include_audio=True, )
        prop = PropBible.objects.create(project=self.project, name="Parcel", description="A blue parcel.",
            anchor_asset=self.asset)
        self.shot.props.add(prop)
        setting = create_variant(actor=self.user, project_id=self.project.pk, entity_kind="prop", entity_id=prop.pk,
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="",
            value={"name": "Parcel", "reference_name": "Parcel image",
                "identity_anchor": "A blue parcel", "appearance_anchor": "Blue parcel on a table",
                "basis_asset_id": None, "basis_asset_sha256": ""}).revisions.get(revision=1)
        self.asset.mime_type = "image/png"; self.asset.save(update_fields=("mime_type",))
        from studio.models import Asset
        video = Asset.objects.get(pk=sources["clips"]["items"][1]["asset_id"])
        video.mime_type = "video/mp4"
        video.save(update_fields=("mime_type",))
        self.originals[2].mime_type = "audio/wav"
        self.originals[2].save(update_fields=("mime_type",))
        for position, (key, kind, purpose, asset, revision) in enumerate((
                ("parcel", "image", "appearance_reference", self.asset, setting),
                ("motion", "video", "motion_camera_reference", video, None),
                ("sound", "audio", "audio_reference", self.originals[2], None))):
            ShotReferenceUse.objects.create(shot=self.shot, use_key=key, position=position, media_kind=kind,
                purpose=purpose, setting_revision=revision, source_mode="existing", asset=asset, asset_sha256=asset.checksum)
        unit_fields = {"unit_key": {"kind": "unit_key"}, "duration_ms": field("shot.duration_ms"),
            "structured_input": literal({"visual": "The parcel opens on the table.",
                "sfx": "The lid makes a soft click.", "music": "A quiet sustained note."}),
            "subject_keys": {"kind": "reference_subjects"},
            **{key: literal(value) for key, value in {"dialogue": [], "voice_profiles": [], "continuity_notes": "",
                "requires_first_frame_continuity": False, "requires_last_frame_continuity": False, "ambient_sound": "Room tone."}.items()}}
        rules = {"content": {"kind": "structured", "template": {"kind": "object", "fields": {
            "schema_version": literal(1), "ordered_units": {"kind": "array", "items": [{"kind": "object", "fields": unit_fields}]},
            **{key: literal("") for key in ("color_script", "extra_direction", "overall_soundscape", "non_diegetic_music")}}}}}
        for target, key in self.profile_keys.items():
            if target.startswith("context."):
                rules[key] = {"kind": "structured", "template": {"kind": "unit_map", "value": literal({"declared_context": target})}}
        for key, group in (("references", "images"), ("clips", "videos"), ("ambient", "audios")):
            rules[key] = {"kind": "reviewed_references", "stage_key": "storyboard_review", "scope": {"kind": "shot"},
                "groups": [{"source_key": group, "purposes": None, "subject_kinds": None,
                    **({"purpose": {"clips": "motion_camera_reference", "ambient": "audio_reference"}[key]}
                        if key != "references" else {})}]}
        self.route = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "input_sources": rules, "parameter_policies": [{"key": key, "policy": "locked_value", "value": value}
                for key, value in self.request_parameters.items()]}
        recipe = ProductionRecipeVersion(recipe_key="v21-entry-test", version=1, name="Isolated routing fixture", status="published")
        self.write(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "storyboard_review", "mode": "review", "approval": "required"},
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "required",
                "routing": {"mode": "fixed", "variant_count": 1, "routes": [self.route], "evaluator": None}}]}, schema_version=3)
        self.run = ProductionRun.objects.create(owner=self.user, project=self.project, recipe_version=recipe, plan_source="recipe",
            status="running", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        self.write(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.write(self.run, "input_snapshot", {"schema_version": 1})
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        self.plan = get_run_execution_plan(self.run)
        review = StageRun.objects.create(production_run=self.run, stage_key="storyboard_review", ordinal=1,
            mode="review", status="succeeded", idempotency_key=str(uuid4()))
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", ordinal=2,
            mode="existing_pipeline", status="running", idempotency_key=str(uuid4()))
        _save_stage_snapshot(review, "output_snapshot", _formal_snapshot(self.run, review, f"storyboard:{self.episode.pk}"))
        ApprovalDecision.objects.create(stage_run=review, revision=1, decision="approve", actor="human", decided_by=self.user,
            output_snapshot_sha256=review.output_snapshot_sha256)
        self.client = APIClient(); self.client.force_authenticate(self.user)

    def test_image_creative_description_survives_slot_without_extra_purpose(self):
        prepared = prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
            scope={"kind": "shot", "id": str(self.shot.pk)}, candidate_number=1, actor=self.user)
        self.assertIsNone(prepared.contract.resolved_inputs["inputs"]["logical_values"]["references"][0]["purpose"])
        self.assertEqual(ShotReferenceUse.objects.get(shot=self.shot, use_key="parcel").purpose, "appearance_reference")
        self.assertFalse(GenerationAttempt.objects.exists())

    def selection_run(self, source):
        """Declared-shot fixture; real command/review/target/preparation from here.

        This does not certify public recipe creation or the upstream writer.
        There is no fake admission/Smoke fact and no model dispatch.
        """
        from studio.services.production_runs import _freeze_stage_config, _freeze_stage_input
        from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
        recipe = ProductionRecipeVersion(recipe_key="selected-image-entry", version=1,
            name="Isolated exact-image route", status="published")
        self.write(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "storyboard", "mode": "deterministic", "approval": "auto"},
            {"key": "storyboard_review", "mode": "review", "approval": "required",
                "shot_reference_selection": {"source": source, "source_stage_key": None,
                    "selection": "manual", "allow_override": True}},
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "auto",
                "routing": {"mode": "fixed", "variant_count": 1, "routes": [self.route], "evaluator": None}}]}, schema_version=3)
        self.run = ProductionRun.objects.create(owner=self.user, project=self.project, recipe_version=recipe,
            plan_source="recipe", status="awaiting_approval", current_stage_key="storyboard_review",
            idempotency_key=str(uuid4()), request_sha256="a" * 64)
        self.write(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.write(self.run, "input_snapshot", {"schema_version": 1})
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        self.plan = get_run_execution_plan(self.run)
        freeze_recipe_request_fixture(self.run, authorized=False)
        storyboard = StageRun.objects.create(production_run=self.run, stage_key="storyboard", ordinal=1,
            mode="deterministic", status="succeeded", idempotency_key=str(uuid4()))
        _save_stage_snapshot(storyboard, "output_snapshot", _formal_snapshot(self.run, storyboard, "declared-shot-fixture"))
        review = StageRun.objects.create(production_run=self.run, stage_key="storyboard_review", ordinal=2,
            mode="review", status="awaiting_approval", idempotency_key=str(uuid4()))
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", ordinal=3,
            mode="existing_pipeline", status="pending", idempotency_key=str(uuid4()))
        _freeze_stage_config(self.run, review)
        _freeze_stage_input(self.run, review)

    def selected_image_request(self, source):
        from hashlib import sha256
        from io import BytesIO
        from django.core.files.base import ContentFile
        from PIL import Image
        from studio.models import Asset
        from studio.services.asset_provenance import ASSET_UPLOAD_SOURCE_INFO
        from studio.services.execution_workflow_requests import verify_execution_workflow_transport
        sibling = Shot.objects.create(episode=self.episode, number=2, title="Untouched sibling",
            media_type="video", duration_ms=5000, include_audio=True)
        sibling_use = ShotReferenceUse.objects.create(shot=sibling, use_key="parcel", position=0,
            media_kind="image", source_mode=source, asset=self.asset, asset_sha256=self.asset.checksum)
        # Every selected source is explicitly of the frozen mode; uploads are
        # real isolated file-backed Assets, never temporary-path bypasses.
        for use in ShotReferenceUse.objects.filter(shot__in=[self.shot, sibling]):
            use.source_mode = source; use.save(update_fields=("source_mode",))
            if source == "upload":
                Asset.objects.filter(pk=use.asset_id).update(source_info=ASSET_UPLOAD_SOURCE_INFO)
        prop = PropBible.objects.get(project=self.project)
        primary = prop.anchor_asset_id
        before = list(ShotReferenceUse.objects.filter(shot=sibling).values())
        buffer = BytesIO(); Image.new("RGB", (12, 18), "red").save(buffer, format="PNG")
        content = buffer.getvalue()
        selected = Asset(owner=self.user, kind="reference", name="Explicit alternate", mime_type="image/png",
            checksum=sha256(content).hexdigest(), source_info=ASSET_UPLOAD_SOURCE_INFO)
        selected.file.save("alternate.png", ContentFile(content))
        self.selection_run(source)
        base = f"/api/v1/production-runs/{self.run.pk}"
        detail = self.client.get(base + "/recipe-shot-references/", {"shot_id": str(self.shot.pk)})
        self.assertEqual(detail.status_code, 200, detail.data)
        use = ShotReferenceUse.objects.get(shot=self.shot, use_key="parcel")
        body = {"action": "select", "stage_key": "storyboard_review", "unit_key": f"shot:{self.shot.pk}",
            "scope": {"kind": "shot", "id": str(self.shot.pk)}, "expected_run_plan_sha256": self.plan.sha256,
            "expected_selection_revision": None, "idempotency_key": "exact-image-change",
            "payload": {"expected_source_snapshot_sha256": detail.data["source_snapshot_sha256"],
                "expected_uses_sha256": detail.data["uses_sha256"], "choices": [{"use_id": str(use.pk),
                    "source_mode": source, "reference_version_id": None, "asset_id": str(selected.pk),
                    "asset_sha256": selected.checksum}]}}
        changed = self.client.post(base + "/manual-actions/", body, format="json")
        self.assertEqual(changed.status_code, 201, changed.data)
        observed = self.client.get(base + "/storyboard-reference-selection/")
        self.assertEqual(observed.status_code, 200, observed.data)
        approved = self.client.post(base + "/approve/", {"stage_key": "storyboard_review", "decision": "approve",
            "expected_source_snapshot_sha256": observed.data["source_snapshot_sha256"],
            "expected_selection_sha256": observed.data["selection_sha256"]}, format="json")
        self.assertEqual(approved.status_code, 200, approved.data)
        preparation = ExecutionPreparation.objects.get(stage_run=self.stage, unit_key=f"shot:{self.shot.pk}")
        self.select_fields(preparation)
        preparation.refresh_from_db()
        preview = compile_prepared_execution_transport(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=self.user)
        compiled = verify_execution_workflow_transport(self.definition, preview.transport)
        facts = preparation.contract.resolved_inputs["inputs"]["logical_values"]["references"]
        self.assertEqual([row["asset_id"] for row in facts], [str(selected.pk)])
        self.assertIsNone(facts[0]["purpose"])
        media = preview.transport.value["payload"]["media_preparation"]
        image = next(row for row in media["bindings"] if row["item_key"] == facts[0]["item_key"])
        self.assertEqual(image["asset_id"], str(selected.pk))
        self.assertEqual(image["media_facts"]["sha256"], selected.checksum)
        self.assertEqual(image["source_sha256"], facts[0]["source_sha256"])
        self.assertEqual(compiled["graph"]["cr_images_0"]["inputs"]["image"], image["token"])
        prop.refresh_from_db()
        self.assertEqual(prop.anchor_asset_id, primary)
        self.assertEqual(list(ShotReferenceUse.objects.filter(shot=sibling).values()), before)
        use.refresh_from_db(); self.assertEqual(use.purpose, "appearance_reference")
        self.assertEqual(prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
            scope={"kind": "shot", "id": str(self.shot.pk)}, candidate_number=1, actor=self.user).pk, preparation.pk)
        replay = self.client.post(base + "/manual-actions/", body, format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], changed.data["id"])
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_existing_image_change_reaches_original_preparation_and_workflow_request(self):
        self.selected_image_request("existing")

    def test_uploaded_image_change_reaches_original_preparation_and_workflow_request(self):
        self.selected_image_request("upload")

    def select_fields(self, preparation):
        for field_key, text in {"detailed_description": "The parcel opens on the table.",
                "overall_soundscape": "The lid makes a soft click.", "non_diegetic_music": "A quiet sustained note."}.items():
            preparation.refresh_from_db()
            row = create_manual_execution_field(preparation_id=preparation.pk, unit_key=f"shot:{self.shot.pk}", field_key=field_key,
                text=text, revision=1, expected_preparation_revision=preparation.revision, actor=self.user)
            preparation.refresh_from_db()
            confirm_execution_field(preparation_id=preparation.pk, field_value_id=row.pk, expected_text_sha256=row.text_sha256,
                expected_source_sha256=row.source_sha256, expected_preparation_revision=preparation.revision,
                select_for_execution=True, expected_selection_revision=None, replace=False, actor=self.user)
        preparation.refresh_from_db()
        return compile_preparation_profile(preparation_id=preparation.pk, expected_preparation_revision=preparation.revision, actor=self.user)

    def test_same_reviewed_inputs_compile_through_asset_manual_and_recipe_preparations(self):
        recipe = prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
            scope={"kind": "shot", "id": str(self.shot.pk)}, candidate_number=1, actor=self.user)
        facts = recipe.contract.resolved_inputs
        # Direct callers provide the same values explicitly, never a caller-authored
        # snapshot or a fake Recipe/approval. Media identity is preserved exactly.
        bound = resolve_recipe_media_sources(definition=self.definition, route=self.route, stage=self.stage, plan=self.plan,
            scope={"kind": "shot", "id": str(self.shot.pk)})["inputs"]
        inputs = {key: deepcopy(bound[key]) if spec["kind"] in {"image", "video", "audio"} else literal(facts["inputs"]["logical_values"][key])
            for spec in self.definition.value["configuration"]["inputs"] for key in [spec["key"]]
            if key in bound or spec["kind"] not in {"image", "video", "audio"}}
        for source in inputs.values():
            for item in source.get("items", []):
                item["scope_ref"] = {"kind": "request"}
        body = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "inputs": inputs, "parameters": self.request_parameters, "manual_channels": {}, "subject_facts": [],
            "source_language": "English", "target_language": "English"}
        counts = SeriesProject.objects.count(), Shot.objects.count()
        response = self.client.post("/api/v1/asset-generation-jobs/", {**body, "job_type": "asset", "media_type": "video",
            "name": "Explicit same inputs", "field_values": []}, format="json", HTTP_IDEMPOTENCY_KEY="v21-parity")
        self.assertEqual(response.status_code, 202, response.data)
        asset = AssetGenerationJob.objects.get(pk=response.data["id"]).execution_preparations.get()
        self.assertEqual(counts, (SeriesProject.objects.count(), Shot.objects.count()))
        from studio.services.execution_smoke import create_instance_smoke
        smoke, smoke_preparation, created = create_instance_smoke(version_id=self.version.pk, actor=self.user,
            client_idempotency_key="v21-first-smoke", expected_definition_sha256=self.definition.sha256,
            fixture={key: value for key, value in body.items() if key not in {"execution_instance_version_id", "expected_definition_sha256"}},
            field_values=[], allow_external_calls=False, budget_limit=None)
        self.assertTrue(created)
        self.assertEqual(smoke.status, "preparing")
        self.assertIsNone(smoke.deadline_started_at)
        catalog = production_stage_catalog()
        response = self.client.post("/api/v1/production-runs/", {"project_id": str(self.project.pk), "plan_source": "manual",
            "idempotency_key": "v21-manual-root", "allow_external_calls": False,
            "config_overrides": {}, "manual_plan": {"stage_catalog_key": catalog.value["key"],
                "expected_stage_catalog_sha256": catalog.sha256, "scope": {"kind": "project"},
                "delivery": {"kind": "episode_masters"}, "initial_content": None}}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        run = ProductionRun.objects.get(pk=response.data["id"])
        response = self.client.post(f"/api/v1/production-runs/{run.pk}/manual-actions/", {"action": "generate",
            "stage_key": "media_generation", "unit_key": f"shot:{self.shot.pk}", "scope": {"kind": "shot", "id": str(self.shot.pk)},
            "expected_run_plan_sha256": get_run_execution_plan(run).sha256, "expected_selection_revision": None,
            "idempotency_key": "v21-manual-inputs", "payload": body}, format="json")
        self.assertEqual(response.status_code, 202, response.data)
        manual = ExecutionPreparation.objects.get(pk=response.data["target_id"])
        results, graphs = [], []
        for preparation in (recipe, asset, manual, smoke_preparation):
            value = preparation.contract.resolved_inputs
            context = value["profile_sources"]["reference_context"]["value"]["context"]
            expected = facts["profile_sources"]["reference_context"]["value"]["context"]
            for key in ("ordered_units", "subject_facts", "mode", "include_audio", "frame_policy"):
                self.assertEqual(context[key], expected[key], key)
            self.assertEqual(value["parameters"]["values"], facts["parameters"]["values"])
            for key in ("references", "clips", "ambient"):
                actual = value["inputs"]["logical_values"][key]
                original = facts["inputs"]["logical_values"][key]
                self.assertEqual(len(actual), len(original))
                for row, previous in zip(actual, original):
                    for field_key in ("item_key", "asset_id", "artifact_id", "purpose", "paired_to", "subject", "media_facts"):
                        self.assertEqual(row[field_key], previous[field_key], field_key)
            results.append(self.select_fields(preparation).compilation)
            preparation.refresh_from_db()
            preview = compile_prepared_execution_transport(preparation_id=preparation.pk,
                expected_preparation_revision=preparation.revision, actor=self.user)
            replay = compile_prepared_execution_transport(preparation_id=preparation.pk,
                expected_preparation_revision=preparation.revision, actor=self.user)
            self.assertEqual(preview, replay)
            from studio.services.execution_workflow_requests import verify_execution_workflow_transport
            compiled = verify_execution_workflow_transport(self.definition, preview.transport)
            media = preview.transport.value["payload"]["media_preparation"]
            for key, expected_value in self.request_parameters.items():
                self.assertEqual(compiled["resolved_semantics"][key], expected_value, key)
            self.assertEqual(compiled["resolved_semantics"]["prompt.positive"], results[-1].assembly["channels"]["positive_prompt"])
            self.assertIsNone(media["classified_groups"]["videos"][0]["paired_audio"])
            self.assertNotIn("paired", value["inputs"]["logical_values"])
            # Only request namespaces and source-bound tokens differ. Replace
            # those exact values with their verified roles, not arbitrary hashes.
            substitutions = {row["token"]: {"media_item": row["item_key"]} for row in media["bindings"]}
            substitutions[f"autoprompt/execution/{preparation.pk}"] = {"output_prefix": "this_preparation"}
            def roles(value):
                if isinstance(value, dict):
                    return {key: roles(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [roles(item) for item in value]
                return substitutions.get(value, value) if isinstance(value, str) else value
            graphs.append(roles(compiled["graph"]))
        # Owner/review/provenance hashes legitimately differ across roots.
        # The same semantic inputs and selected contributions must not change text.
        for result in results[1:]:
            for key in ("channels", "structured_fields"):
                self.assertEqual(results[0].assembly[key], result.assembly[key])
        for graph in graphs[1:]:
            self.assertEqual(graphs[0], graph)
        self.assertEqual(list(results[0].assembly["structured_fields"]), ["subject_definitions", "summary", "retention_analysis",
            "detailed_description", "overall_soundscape", "non_diegetic_music"])
        self.assertIn("3 supplied references", results[0].assembly["structured_fields"]["summary"])
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertIsNone(run.recipe_version_id)
        self.assertEqual(counts, (SeriesProject.objects.count(), Shot.objects.count()))
