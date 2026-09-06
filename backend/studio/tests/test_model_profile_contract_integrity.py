from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from studio.models import ModelProfileVersion
from studio.services.async_media_contract import deployment_target_payload
from studio.services.json_contracts import json_contract
from studio.services.model_profile_contracts import (
    verified_frozen_model_profile_specification,
    verified_model_profile_specification,
)
from studio.services.prompt_normalization import route_state
from studio.services.production_runs import _freeze_stage_routing
from studio.tests.factories import create_ready_prompt_normalization_test_deployment


def _profile(specification: object) -> ModelProfileVersion:
    contract = json_contract(specification)
    return ModelProfileVersion(
        specification=contract.value,
        specification_schema_version=contract.schema_version,
        specification_size_bytes=contract.size_bytes,
        specification_sha256=contract.sha256,
    )


class ModelProfileSpecificationIntegrityTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        limit_patch = patch(
            "studio.services.model_profile_contracts._stage_snapshot_max_bytes",
            return_value=1024 * 1024,
        )
        limit_patch.start()
        self.addCleanup(limit_patch.stop)

    def test_valid_contract_returns_the_verified_object(self):
        profile = _profile({"schema_version": 1, "capabilities": ["script.generate"]})

        self.assertEqual(
            verified_model_profile_specification(profile),
            profile.specification,
        )

    def test_equal_size_body_drift_fails_closed(self):
        profile = _profile({"schema_version": 1, "mode": "low"})
        profile.specification = {"schema_version": 1, "mode": "max"}

        with self.assertRaisesMessage(
            ValidationError,
            "size/checksum 与正文不一致",
        ):
            verified_model_profile_specification(profile)

    def test_size_or_sha_metadata_drift_fails_closed(self):
        profile = _profile({"schema_version": 1})
        profile.specification_size_bytes += 1
        with self.assertRaises(ValidationError):
            verified_model_profile_specification(profile)

        profile = _profile({"schema_version": 1})
        profile.specification_sha256 = "0" * 64
        with self.assertRaises(ValidationError):
            verified_model_profile_specification(profile)

    def test_non_object_json_fails_closed_after_integrity_verification(self):
        profile = _profile(["script.generate"])

        with self.assertRaisesMessage(
            ValidationError,
            "specification 必须是 JSON 对象",
        ):
            verified_model_profile_specification(profile)

    def test_frozen_route_must_prove_the_live_verified_source(self):
        profile = _profile({"schema_version": 1, "mode": "low"})
        route = {
            "model_specification": deepcopy(profile.specification),
            "model_specification_sha256": profile.specification_sha256,
            "frozen_model_specification_sha256": profile.specification_sha256,
        }
        self.assertEqual(
            verified_frozen_model_profile_specification(profile, route),
            profile.specification,
        )

        route["model_specification"] = {"schema_version": 1, "mode": "max"}
        route["frozen_model_specification_sha256"] = json_contract(
            route["model_specification"],
        ).sha256
        with self.assertRaisesMessage(
            ValidationError,
            "来源证明无效",
        ):
            verified_frozen_model_profile_specification(profile, route)


class ModelProfileRuntimeIntegrityTests(TestCase):
    def test_prompt_route_and_deployment_target_reject_raw_database_drift(self):
        deployment = create_ready_prompt_normalization_test_deployment()
        state, reason, route = route_state(deployment)
        self.assertEqual((state, reason), ("ready", "ready"))
        self.assertIsNotNone(route)
        deployment_target_payload(deployment)

        drifted = deepcopy(deployment.profile_version.specification)
        drifted["prompt_normalization"]["max_output_tokens"] = 9_192
        ModelProfileVersion.objects.filter(
            pk=deployment.profile_version_id,
        ).update(specification=drifted)
        deployment.refresh_from_db()

        with patch(
            "studio.services.prompt_normalization.driver_for",
        ) as driver_for_mock:
            state, reason, route = route_state(deployment)

        self.assertEqual(state, "unavailable")
        self.assertEqual(reason, "model_profile_contract_invalid")
        self.assertIsNone(route)
        driver_for_mock.assert_not_called()
        with self.assertRaises(ValidationError):
            deployment_target_payload(deployment)

    def test_production_route_freeze_rejects_raw_database_drift(self):
        deployment = create_ready_prompt_normalization_test_deployment()
        drifted = deepcopy(deployment.profile_version.specification)
        drifted["prompt_normalization"]["timeout_seconds"] = 181.0
        ModelProfileVersion.objects.filter(
            pk=deployment.profile_version_id,
        ).update(specification=drifted)

        with self.assertRaises(ValidationError):
            _freeze_stage_routing({
                "mode": "fixed",
                "routes": [{
                    "model_deployment_id": str(deployment.id),
                }],
                "variant_count": 1,
                "evaluator": None,
            }, stage_key="script_generate")
