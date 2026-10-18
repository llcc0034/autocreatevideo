"""Real model authoring API for transport-only script policies, no network."""
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import ModelProfileVersion, PlatformLifecycleAuditEvent, ProviderProfile
from studio.platform_serializers import ModelProfileRegistryOptionsSerializer
from studio.services.json_contracts import assign_json_contract
from studio.services.model_profile_options import model_profile_registry_options
from studio.services.script_model_policies import script_contract_editor_options, validate_script_model_policy
from studio.services.script_generation_contract import validate_script_generation_model_profile
from studio.services.script_structure import validate_script_structure_model_profile


class ScriptContractAuthoringTests(TestCase):
    def setUp(self):
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("No network")))
        self.enterContext(patch("studio.services.provider_registry.resolve_credential", side_effect=AssertionError("No credentials")))
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user(username="script-authoring-test", is_staff=True, is_superuser=True))
        self.provider = ProviderProfile(key="script-authoring-test", name="Isolated", driver_type="openai_compatible", modalities="text")
        assign_json_contract(self.provider, "driver_config", {"base_url": "http://localhost", "chat_path": "/v1/chat/completions", "timeout_seconds": 30, "capability_ttl_seconds": 3600})
        self.provider.save()
        self.url = "/api/v1/model-profile-versions/"

    def specification(self):
        return {"schema_version": 1, "model_family": "script-authoring-test", "capabilities": ["script.generate", "script.structure"],
                "series_concept": {"timeout_seconds": 30, "chat_request_options": {"temperature": 0, "thinking": {"type": "disabled"}}},
                "script_structure": {"timeout_seconds": 31.5, "chat_request_options": {"temperature": None, "response_format": {"type": "json_object"}}}}

    def create(self, specification):
        return self.client.post(self.url, {"profile_key": "script-authoring-test", "provider": str(self.provider.pk),
            "modality": "text", "model_identifier": "isolated", "specification": specification}, format="json")

    def counts(self):
        return ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()

    def test_catalog_only_describes_transport_and_uses_runtime_timeout(self):
        catalog = script_contract_editor_options()
        self.assertEqual(set(catalog), {"schema_version", "description", "policies"})
        self.assertEqual(catalog["schema_version"], 3)
        self.assertEqual(ModelProfileRegistryOptionsSerializer(model_profile_registry_options()).data["script_contracts"], catalog)
        response = self.client.get(self.url + "options/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["script_contracts"], catalog)
        for row in catalog["policies"]:
            self.assertEqual(set(row), {"key", "operation", "label", "number_fields"})
            self.assertEqual([item["key"] for item in row["number_fields"]], ["timeout_seconds"])
            self.assertEqual(validate_script_model_policy(self.specification()[row["key"]], key=row["key"]), self.specification()[row["key"]])
        with patch("studio.services.script_model_policies.boundary_number", return_value=25):
            self.assertEqual(script_contract_editor_options()["policies"][0]["number_fields"][0]["maximum"], 25)
            with self.assertRaises(ValidationError):
                validate_script_model_policy(self.specification()["series_concept"], key="series_concept")

    def test_validation_rejects_old_fields_missing_transport_and_invalid_numbers(self):
        for key in ("series_concept", "script_structure"):
            policy = self.specification()[key]
            invalids = [None, {}, {**policy, "chat_request_options": None}]
            invalids += [{**policy, "timeout_seconds": value} for value in (True, None, 0, -1, "30", float("inf"), float("nan"))]
            invalids += [{**policy, name: {}} for name in ("field_contract", "reference_policy", "minimum_shot_duration_ms",
                "maximum_shot_duration_ms", "max_input_bytes", "unknown")]
            invalids += [{name: value for name, value in policy.items() if name != missing} for missing in policy]
            for value in invalids:
                with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                    validate_script_model_policy(value, key=key)
            self.assertEqual(validate_script_model_policy({**policy, "timeout_seconds": 0.5}, key=key)["timeout_seconds"], 0.5)

    def test_old_model_fields_cannot_be_saved_or_silently_converted(self):
        before = self.counts()
        for key in ("series_concept", "script_structure"):
            specification = self.specification()
            specification[key]["field_contract"] = {"title": {"required": True}}
            response = self.create(specification)
            self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(self.counts(), before)

    def test_save_publish_successor_roundtrip_and_failure_are_atomic(self):
        original = self.specification()
        invalid = deepcopy(original)
        invalid["script_structure"]["timeout_seconds"] = 0
        before = self.counts()
        self.assertEqual(self.create(invalid).status_code, 400)
        self.assertEqual(self.counts(), before)
        response = self.create(original)
        self.assertEqual(response.status_code, 201, response.data)
        detail = f"{self.url}{response.data['id']}/"
        changed = deepcopy(original)
        changed["series_concept"]["chat_request_options"]["temperature"] = 0.3
        changed["script_structure"]["timeout_seconds"] = 22.5
        response = self.client.patch(detail, {"specification": changed}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        for key in ("series_concept", "script_structure"):
            invalid = deepcopy(changed)
            del invalid[key]["chat_request_options"]
            before = self.counts()
            self.assertEqual(self.client.patch(detail, {"specification": invalid}, format="json").status_code, 400)
            self.assertEqual(self.client.get(detail).data["specification"], changed)
            self.assertEqual(self.counts(), before)
        response = self.client.post(detail + "publish/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        model = ModelProfileVersion.objects.get(pk=response.data["id"])
        self.assertEqual(validate_script_generation_model_profile(model), changed["series_concept"])
        self.assertEqual(validate_script_structure_model_profile(model), changed["script_structure"])
        old_sha = model.specification_sha256
        self.assertEqual(self.client.patch(detail, {"specification": original}, format="json").status_code, 400)
        before = self.counts()
        self.assertEqual(self.client.post(detail + "draft-next/", {"specification": invalid}, format="json").status_code, 400)
        self.assertEqual(self.counts(), before)
        response = self.client.post(detail + "draft-next/", {"specification": original}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["specification"], original)
        self.assertEqual(self.client.get(detail).data["specification_sha256"], old_sha)
        draft = ModelProfileVersion.objects.get(pk=response.data["id"])
        broken = deepcopy(original)
        broken["script_structure"]["reference_policy"] = {}
        assign_json_contract(draft, "specification", broken)
        draft.save()
        before = self.counts()
        response = self.client.post(f"{self.url}{draft.pk}/publish/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "draft")
        self.assertEqual(draft.specification, broken)
        self.assertEqual(self.counts(), before)
