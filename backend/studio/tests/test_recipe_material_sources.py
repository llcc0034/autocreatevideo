"""Frozen result-source authoring and zero-call waiting on isolated M3 facts."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings

from studio.models import AssetReferenceVersion, ExecutionPreparation, GenerationAttempt, ProductionMaterialFulfillment
from studio.services.production_recipe_contracts import recipe_stage_declarations, validate_recipe_definition
from studio.services.production_recipe_material_sources import recipe_material_source
from studio.services.production_recipe_media_stages import advance_recipe_media_stage
from studio.tests import test_recipe_material_fulfillment_api as fixtures
from studio.tests.test_asset_management_api import image_upload
from studio.tests.test_data_structures import StructureFixture


class MaterialSourceContractTests(SimpleTestCase):
    def stage(self, **changes):
        return {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "auto", **changes}

    def definition(self, stage):
        return {"schema_version": 3, "interaction_mode": "assisted", "stages": [stage]}

    def test_generation_default_is_read_only_and_explicit_sources_do_not_inherit_it(self):
        stage = self.stage()
        before = deepcopy(stage)
        self.assertEqual(recipe_material_source(stage, "primary"), "generate")
        self.assertEqual(stage, before)
        for source in ("existing", "upload"):
            stage = self.stage(material_sources={"primary": source, "variant": source})
            self.assertEqual(recipe_stage_declarations(self.definition(stage)), [stage])
            self.assertEqual(recipe_material_source(stage, "variant"), source)

    def test_invalid_sources_extra_keys_wrong_duties_and_idle_routes_are_rejected(self):
        values = [None, {}, {"primary": "existing"}, {"primary": "existing", "variant": "auto"},
            {"primary": "existing", "variant": []}, {"primary": "existing", "variant": "upload", "fallback": "generate"}]
        for sources in values:
            with self.subTest(sources=sources), self.assertRaises(ValidationError):
                recipe_stage_declarations(self.definition(self.stage(material_sources=sources)))
        valid = {"primary": "existing", "variant": "upload"}
        for changes in ({"routing": {}}, {"variant_routing": {}}, {"mode": "manual"}, {"key": "media_generation"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                recipe_stage_declarations(self.definition(self.stage(material_sources=valid, **changes)))


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class MaterialSourceStageTests(StructureFixture):
    setUp = fixtures.RecipeMaterialFulfillmentApiTests.setUp
    tearDown = fixtures.RecipeMaterialFulfillmentApiTests.tearDown
    facts = fixtures.RecipeMaterialFulfillmentApiTests.facts
    create_job = fixtures.RecipeMaterialFulfillmentApiTests.create_job
    primary_version = fixtures.RecipeMaterialFulfillmentApiTests.primary_version
    write = fixtures.RecipeMaterialFulfillmentApiTests.write
    input = staticmethod(fixtures.RecipeMaterialFulfillmentApiTests.input)
    configure_variant_sources = fixtures.RecipeMaterialFulfillmentApiTests.configure_variant_sources
    target = fixtures.RecipeMaterialFulfillmentApiTests.target
    prepare = fixtures.RecipeMaterialFulfillmentApiTests.prepare
    payload = fixtures.RecipeMaterialFulfillmentApiTests.payload
    inspect = fixtures.RecipeMaterialFulfillmentApiTests.inspect
    select = fixtures.RecipeMaterialFulfillmentApiTests.select

    def configure_declaration(self, declaration):
        declaration["material_sources"] = {"primary": "existing", "variant": "upload"}
        declaration.pop("routing")
        declaration.pop("variant_routing")

    def advance(self):
        with patch("studio.services.production_recipe_media_stages.prepare_recipe_media_unit",
                side_effect=AssertionError("No generated preparation for existing/upload sources")), patch(
                "studio.services.production_recipe_media_stages.advance_execution_preparation",
                side_effect=AssertionError("No model advancement for existing/upload sources")):
            return advance_recipe_media_stage(self.run, self.stage)

    def test_no_route_publishes_without_model_requirements_and_waits_repeatedly_without_writes(self):
        definition = self.definition_for_validation()
        self.assertEqual(validate_recipe_definition(definition, require_complete=True), definition["stages"])
        for _ in range(2):
            self.assertIsNone(self.advance())
            self.assertEqual(self.stage.reason_code, "waiting_material_source")
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.assertEqual(self.facts(), self.before, "waiting changes only the caller-owned reason until stage save")

    def definition_for_validation(self):
        return {"schema_version": 3, "interaction_mode": "assisted", "stages": [self.declaration]}

    def test_primary_and_two_uploads_resume_zero_call_completion_without_fake_generation_or_review(self):
        primary = self.payload(self.primary_setting, self.primary.series)
        self.assertEqual(self.inspect(primary).data["source_mode"], "existing")
        self.assertEqual(self.select(primary).status_code, 201)
        self.assertIsNone(self.advance())
        self.assertIn("2 个素材需求", self.stage.reason)
        upload = self.client.post("/api/v1/assets/", {"name": "isolated reused result", "kind": "reference",
            "file": image_upload()}, format="multipart")
        self.assertEqual(upload.status_code, 201, upload.data)
        for setting, series in zip(self.settings, self.series):
            payload = self.payload(setting, series, source="upload", asset_id=upload.data["id"])
            self.assertEqual(self.inspect(payload).data["source_mode"], "upload")
            self.assertEqual(self.select(payload).status_code, 201)
            self.assertEqual(self.select(payload).status_code, 200)
        self.assertEqual(self.advance(), "material-fulfillments:3")
        self.assertEqual(self.advance(), "material-fulfillments:3")
        self.assertEqual(ProductionMaterialFulfillment.objects.count(), 3)
        self.assertFalse(AssetReferenceVersion.objects.filter(generated_from__isnull=False).exists())
        self.assertEqual(self.facts(), self.before)

    def test_source_mismatch_and_direct_preparation_do_not_bypass_waiting(self):
        response = self.select()  # Variant requires an actual upload, not an existing label.
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        with self.assertRaisesMessage(ValidationError, "不能绕过待来源"):
            self.prepare()
        self.assertIsNone(self.advance())
        self.assertEqual(self.facts(), self.before)

    def test_new_editor_options_expose_only_implemented_result_sources(self):
        from studio.services.production_runs import production_recipe_editor_options
        self.assertEqual([row["value"] for row in production_recipe_editor_options()["material_source_options"]],
            ["generate", "existing", "upload"])

    def test_shared_preparation_writer_rejects_unfulfilled_non_generation_roles(self):
        from studio.services.execution_preparations import create_execution_instance_preparation
        from studio.services.json_contracts import json_contract
        for setting in (self.primary_setting, *self.settings):
            with self.subTest(setting=setting.pk), self.assertRaisesMessage(ValidationError, "不能绕过待来源"):
                create_execution_instance_preparation(owner_ref=("stage_run_id", self.stage.pk),
                    version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
                    unit_key=f"appearance:{setting.variant_id}", request_key="route:0:candidate:1",
                    intent=json_contract({}), actor=self.user,
                    resolve_facts=lambda *_: self.fail("Non-generation sources must not resolve inputs"))
        self.assertEqual(self.facts(), self.before)
