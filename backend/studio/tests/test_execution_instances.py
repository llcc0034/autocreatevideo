from studio.tests.field_definition_fixtures import create_field_definition
"""Isolated registration/reference checks, not production or real Smoke evidence."""
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import (
    ExecutionAuxiliaryBinding, ExecutionInputSpec, ExecutionInputTarget,
    ExecutionParameterSpec, ExecutionVersionContract, ModelDeployment,
    ModelExecutionInstance, ModelExecutionInstanceVersion, ModelProfileVersion,
    PlatformLifecycleAuditEvent, FieldDefinition, PromptProfileFieldRule, PromptProfileVersion, ProviderProfile, WorkflowSemanticBinding, WorkflowTemplate, WorkflowTemplateVersion,
)
from studio.services.execution_instances import (
    clone_execution_draft, create_execution_draft, create_execution_instance, update_execution_draft,
    resolve_execution_resources, validate_execution_parameter_spec,
    validate_execution_input_mapping, validate_input_semantic_contract,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.prompt_profiles import PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION, refresh_prompt_profile_contract
from studio.tests.factories import conditioning_contract


def semantic_contract():
    return {
        "schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": [],
        "allowed_scope_kinds": [], "paired_with_input_key": None, "allow_repeated_asset": False,
    }


class ExecutionParameterTests(SimpleTestCase):
    def setUp(self):
        patcher = patch("studio.services.execution_instances.boundary_int", return_value=65536)
        self.boundary = patcher.start()
        self.addCleanup(patcher.stop)

    def spec(self, **changes):
        fields = {
            "key": "sample.parameter", "value_type": "integer", "policy": "overridable",
            "minimum": 5, "maximum": 90, "step": 17, "remainder": 5,
            "enum_values": [], "default_value": 22,
        }
        fields.update(changes)
        return ExecutionParameterSpec(**fields)

    def test_integer_alignment_and_explicit_default(self):
        validate_execution_parameter_spec(self.spec())
        validate_execution_parameter_spec(self.spec(default_value=None))
        self.assertIsNone(self.spec(default_value=None).default_value)

    def test_invalid_ranges_and_defaults(self):
        cases = [
            {"minimum": True}, {"maximum": 4}, {"step": 0}, {"remainder": 17},
            {"step": None}, {"default_value": True}, {"default_value": 23},
            {"default_value": 91}, {"default_value": 4}, {"default_value": 22.0},
            {"maximum": float("inf")}, {"enum_values": [22, 22]},
            {"enum_values": [True]}, {"enum_values": [39]}, {"enum_values": {}},
            {"locked_value": 22}, {"policy": "locked"}, {"policy": "unknown"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_execution_parameter_spec(self.spec(**changes))

    def test_locked_false_is_a_real_value(self):
        spec = self.spec(
            value_type="boolean", minimum=None, maximum=None, step=None, remainder=None,
            default_value=None, policy="locked", locked_value=False,
        )
        validate_execution_parameter_spec(spec)
        spec.locked_value = 0
        with self.assertRaises(ValidationError):
            validate_execution_parameter_spec(spec)

    def test_decimal_step_has_no_binary_float_tolerance(self):
        validate_execution_parameter_spec(self.spec(
            value_type="number", minimum=0, maximum=1, step=0.1, remainder=None, default_value=0.3,
        ))
        with self.assertRaises(ValidationError):
            validate_execution_parameter_spec(self.spec(
                value_type="number", minimum=0, maximum=1, step=0.1, remainder=None, default_value=0.31,
            ))

    def test_string_and_boolean_cannot_have_numeric_constraints(self):
        for kind in ("string", "boolean"):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                validate_execution_parameter_spec(self.spec(value_type=kind))

    def test_nonfinite_and_oversized_values_rejected(self):
        for value in (float("inf"), float("nan")):
            with self.assertRaises(ValidationError):
                validate_execution_parameter_spec(self.spec(value_type="number", default_value=value))
        with self.assertRaises(ValidationError):
            validate_execution_parameter_spec(self.spec(
                value_type="string", minimum=None, maximum=None, step=None, remainder=None,
                default_value="x" * 65536,
            ))

    def test_semantic_shape_is_explicit_and_does_not_mutate(self):
        value = semantic_contract()
        before = deepcopy(value)
        validate_input_semantic_contract(value)
        self.assertEqual(value, before)
        for changes in (
            {"schema_version": True}, {"schema_version": 2}, {"asset_id": "not-configuration"},
            {"allowed_purposes": ["image", "image"]}, {"allowed_purposes": [{}]},
            {"allow_repeated_asset": 1}, {"paired_with_input_key": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_input_semantic_contract({**value, **changes})

    def test_configured_limit_can_tighten_without_raising_storage_cap(self):
        from studio.services.execution_instances import execution_configuration_max_bytes

        self.boundary.return_value = 100
        with self.assertRaises(ValidationError):
            validate_input_semantic_contract(semantic_contract())
        self.boundary.assert_called_with("security.snapshot.stage_max_bytes")
        self.boundary.return_value = 100000
        self.assertEqual(execution_configuration_max_bytes(), 65536)

    def test_subject_and_scope_options_are_the_only_supported_fact_kinds(self):
        from studio.services.execution_input_catalog import execution_input_identity_options

        options = execution_input_identity_options()
        value = {
            **semantic_contract(),
            "allowed_subject_kinds": [row["value"] for row in options["subject_kinds"]],
            "allowed_scope_kinds": [row["value"] for row in options["scope_kinds"]],
        }
        validate_input_semantic_contract(value)
        for field in ("allowed_subject_kinds", "allowed_scope_kinds"):
            with self.subTest(field=field), self.assertRaisesMessage(ValidationError, "未注册"):
                validate_input_semantic_contract({**value, field: ["invented"]})
        options["subject_kinds"][0]["value"] = "mutated"
        self.assertNotEqual(options, execution_input_identity_options())


class ExecutionRegistryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = get_user_model().objects.create_user(username="execution-fixture-admin", is_staff=True)
        cls.provider = ProviderProfile.objects.create(
            key="execution-fixture", name="Execution fixture", driver_type="comfyui",
        )
        cls.model = ModelProfileVersion(
            profile_key="execution-fixture", version=1, model_identifier="fixture",
            modality="video", provider=cls.provider, status="published",
        )
        assign_json_contract(cls.model, "specification", {
            "model_family": "fixture_family", "capabilities": ["video.reference_to_video"],
            "conditioning_contract": conditioning_contract("Ref2VA"),
        })
        cls.model.save()
        cls.deployment = ModelDeployment.objects.create(
            key="execution-fixture", name="Fixture deployment", profile_version=cls.model,
        )
        cls.template = WorkflowTemplate.objects.create(
            slug="execution-fixture", name="Fixture graph", output_kind="video",
        )
        cls.workflow = WorkflowTemplateVersion.objects.create(
            template=cls.template, version=1, provider=cls.provider,
            model_profile_version=cls.model, validated_deployment=cls.deployment,
            status="published", input_contract={
                "capability": "video.reference_to_video",
                "contract_projector": {"version_id": "fixture-projector", "definition_sha256": "a" * 64},
                "parameters": {},
                "classified_references": {"target_node_id": "fixture-node", "groups": {
                    "images": {"media_kind": "image", "min_items": 0, "max_items": 3, "input_names": ["ref_0", "ref_1", "ref_2"]},
                }},
            },
        )
        cls.profile = PromptProfileVersion.objects.create(
            profile_key="execution-fixture", version=21, name="Fixture prompt",
            model_family="fixture_family", operation="video.generate", mode="Ref2VA",
            contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        )
        refresh_prompt_profile_contract(cls.profile)
        cls.profile.status = "published"
        cls.profile.save(update_fields=("status",))
        cls.instance = ModelExecutionInstance.objects.create(key="execution-fixture", name="Fixture")

    def version(self, **changes):
        fields = {
            "instance": self.instance, "version": 1, "deployment": self.deployment,
            "operation": "video.reference_to_video", "mode": "Ref2VA", "prompt_mode": "profile",
            "prompt_profile_version": self.profile, "workflow_version": self.workflow,
        }
        fields.update(changes)
        return ModelExecutionInstanceVersion(**fields)

    def resolve(self, version):
        # Only reference-consistency tests isolate the graph verifier. Separate
        # test below checks that real graph-integrity failures are not swallowed.
        with patch("studio.services.execution_instances.verify_workflow_version_integrity") as verify:
            result = resolve_execution_resources(version)
            verify.assert_called_once_with(version.workflow_version)
            return result

    def test_different_operation_names_are_preserved(self):
        result = self.resolve(self.version())
        self.assertEqual(result["execution"]["operation"], "video.reference_to_video")
        self.assertEqual(result["prompt_profile"]["operation"], "video.generate")
        self.assertEqual(result["prompt_profile"]["id"], str(self.profile.pk))
        self.assertNotIn("can_submit", result)
        self.assertFalse(ModelExecutionInstanceVersion.objects.exists())

    def test_missing_or_wrong_model_capability_is_rejected(self):
        for capabilities in (None, [], ["video.generate"], ["video.reference_to_video "]):
            with self.subTest(capabilities=capabilities):
                assign_json_contract(self.model, "specification", {
                    **self.model.specification, "capabilities": capabilities,
                })
                with self.assertRaises(ValidationError):
                    resolve_execution_resources(self.version())

    def test_same_operation_name_does_not_override_mode_mismatch(self):
        self.replace_profile(operation="video.reference_to_video", mode="DifferentMode")
        with self.assertRaisesMessage(ValidationError, "非空模式"):
            self.resolve(self.version())

    def replace_profile(self, **changes):
        fields = {
            "profile_key": "alternate-fixture", "version": 1, "name": "Alternate fixture",
            "model_family": "fixture_family", "operation": "video.generate", "mode": "Ref2VA",
            "contract_schema_version": PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
        }
        fields.update(changes)
        self.profile = PromptProfileVersion.objects.create(**fields)
        refresh_prompt_profile_contract(self.profile)
        self.profile.status = "published"
        self.profile.save(update_fields=("status",))

    def test_wrong_family_rejected(self):
        self.replace_profile(model_family="another_family")
        with self.assertRaisesMessage(ValidationError, "模型族不一致"):
            self.resolve(self.version())

    def test_empty_mode_does_not_select_first_mode(self):
        with self.assertRaises(ValidationError):
            self.resolve(self.version(mode=""))

    def test_missing_workflow_rejected_for_comfyui(self):
        with self.assertRaisesMessage(ValidationError, "ComfyUI必须选择"):
            resolve_execution_resources(self.version(workflow_version=None))

    def test_workflow_exact_capability_required(self):
        for capability in (None, "video.text_to_video"):
            self.workflow.input_contract["capability"] = capability
            with self.subTest(capability=capability), self.assertRaises(ValidationError):
                self.resolve(self.version())

    def test_wrong_workflow_model_provider_and_deployment_rejected(self):
        from uuid import uuid4

        for field in ("model_profile_version_id", "provider_id", "validated_deployment_id"):
            original = getattr(self.workflow, field)
            setattr(self.workflow, field, uuid4())
            with self.subTest(field=field), self.assertRaises(ValidationError):
                resolve_execution_resources(self.version())
            setattr(self.workflow, field, original)

    def test_draft_dependencies_rejected(self):
        for resource in (self.model, self.workflow, self.profile):
            resource.status = "draft"
            with self.subTest(resource=type(resource).__name__), self.assertRaises(ValidationError):
                self.resolve(self.version())
            resource.status = "published"

    def test_real_graph_integrity_is_required(self):
        with self.assertRaises(ValidationError):
            resolve_execution_resources(self.version())

    def test_model_and_profile_hash_drift_rejected(self):
        self.model.specification["extra"] = "unfrozen change"
        with self.assertRaises(ValidationError):
            resolve_execution_resources(self.version())
        del self.model.specification["extra"]
        self.profile.channel_limits = {"positive_prompt": {"max_chars": 123}}
        with self.assertRaises(ValidationError):
            self.resolve(self.version())

    def test_manual_and_none_do_not_resolve_hidden_profiles(self):
        for prompt_mode in ("manual", "none"):
            with self.subTest(prompt_mode=prompt_mode):
                self.assertIsNone(self.resolve(self.version(prompt_mode=prompt_mode, prompt_profile_version=None))["prompt_profile"])
                with self.assertRaises(ValidationError):
                    self.resolve(self.version(prompt_mode=prompt_mode))

    def test_resources_can_be_shared_without_reverse_one_to_one(self):
        first = self.version()
        first.save()
        other = ModelExecutionInstance.objects.create(key="execution-other", name="Other")
        self.version(instance=other).save()
        self.assertEqual(self.deployment.execution_versions.count(), 2)
        self.assertEqual(self.profile.execution_versions.count(), 2)
        self.assertEqual(self.workflow.execution_versions.count(), 2)

    def test_instance_key_is_immutable_for_save_and_bulk_update(self):
        self.instance.key = "changed-key"
        with self.assertRaises(ValidationError):
            self.instance.save()
        with self.assertRaises(ValidationError):
            ModelExecutionInstance.objects.filter(pk=self.instance.pk).update(key="changed-key")

    def test_version_database_constraints(self):
        self.version().save()
        cases = [
            {}, {"version": 0}, {"version": 2, "revision": 0},
            {"version": 2, "prompt_profile_version": None},
            {"version": 2, "prompt_mode": "manual"},
            {"version": 2, "status": "published"},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                self.version(**changes).save()

    def input_spec(self, version, **changes):
        fields = {
            "version": version, "key": "reference_images", "position": 0,
            "kind": "image", "required": True, "min_items": 1, "max_items": 3,
            "semantic_contract": semantic_contract(),
        }
        fields.update(changes)
        return ExecutionInputSpec(**fields)

    def test_input_count_and_uniqueness_constraints(self):
        version = self.version()
        version.save()
        for changes in ({"min_items": 4}, {"min_items": 0}, {"kind": "text"}, {"kind": "json"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                self.input_spec(version, **changes).save()
        self.input_spec(version).save()
        for changes in ({"position": 1}, {"key": "other_input"}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                self.input_spec(version, **changes).save()

    def test_exactly_one_primary_target_at_most_and_never_profile(self):
        version = self.version()
        version.save()
        spec = self.input_spec(version)
        spec.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExecutionInputTarget.objects.create(input_spec=spec, target_kind="profile_input", target_key="context", is_primary=True)
        ExecutionInputTarget.objects.create(input_spec=spec, target_kind="workflow_slot", target_key="images", is_primary=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExecutionInputTarget.objects.create(input_spec=spec, target_kind="operation_input", target_key="images", is_primary=True)
        ExecutionInputTarget.objects.create(input_spec=spec, target_kind="profile_input", target_key="context", is_primary=False)

    def test_auxiliary_cannot_reference_itself_or_repeat_purpose(self):
        version = self.version()
        version.save()
        other = self.version(version=2)
        other.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExecutionAuxiliaryBinding.objects.create(version=version, purpose="generate_fields", target_version=version)
        ExecutionAuxiliaryBinding.objects.create(version=version, purpose="generate_fields", target_version=other)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExecutionAuxiliaryBinding.objects.create(version=version, purpose="generate_fields", target_version=other)

    def generated_field_fixture(self):
        field = create_field_definition(key="fixture-field", label="Fixture", category="fixture", value_type="long_text")
        create_profile_field_rule(
            profile_version=self.profile, field_definition=field, source_selector="request.text",
            position=1, channel="positive_prompt", generation_mode="model_generated",
        )
        self.profile.status = "draft"
        refresh_prompt_profile_contract(self.profile)
        self.profile.status = "published"
        self.profile.save(update_fields=("status",))

    def leaf_fixture(self, *, operation="prompt.fields.generate", status="published"):
        from django.utils import timezone

        provider = ProviderProfile.objects.create(key="leaf-fixture", name="Leaf", driver_type="openai_compatible")
        model = ModelProfileVersion(profile_key="leaf-fixture", version=1, provider=provider, modality="text", model_identifier="leaf", status="published")
        assign_json_contract(model, "specification", {"capabilities": [operation]})
        model.save()
        deployment = ModelDeployment.objects.create(key="leaf-fixture", name="Leaf", profile_version=model)
        instance = ModelExecutionInstance.objects.create(key="leaf-fixture", name="Leaf")
        return ModelExecutionInstanceVersion.objects.create(
            instance=instance, version=1, deployment=deployment, operation=operation,
            prompt_mode="manual", status=status,
            published_at=timezone.now() if status == "published" else None,
            published_by=self.actor if status == "published" else None,
        )

    def test_auxiliary_draft_authoring_requires_explicit_published_matching_leaf(self):
        from studio.services.execution_instances import validate_execution_auxiliary_references

        self.generated_field_fixture()
        draft = self.managed_draft()
        with self.assertRaisesMessage(ValidationError, "尚未配置"):
            validate_execution_auxiliary_references(draft, require_complete=True)
        leaf = self.leaf_fixture()
        draft = self.update_draft(draft, {"auxiliaries": [{"purpose": "generate_fields", "target_version_id": str(leaf.pk)}]})
        self.assertEqual(validate_execution_auxiliary_references(draft, require_complete=True)[0]["version_id"], str(leaf.pk))
        for changes in ({"prompt_mode": "manual", "prompt_profile_version": None}, {"auxiliaries": [{"purpose": "translate_fields", "target_version_id": str(leaf.pk)}]}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.update_draft(draft, changes, expected_revision=2)
        draft.refresh_from_db()
        self.assertEqual(draft.revision, 2)
        self.assertEqual(draft.auxiliaries.get().target_version_id, leaf.pk)

    def test_auxiliary_wrong_operation_and_draft_are_rejected(self):
        self.generated_field_fixture()
        draft = self.managed_draft()
        leaf = self.leaf_fixture(operation="script.generate")
        payload = {"auxiliaries": [{"purpose": "generate_fields", "target_version_id": str(leaf.pk)}]}
        with self.assertRaisesMessage(ValidationError, "prompt.fields.generate"):
            self.update_draft(draft, payload)
        ModelExecutionInstanceVersion.objects.filter(pk=leaf.pk).update(status="draft", published_at=None, published_by=None)
        with self.assertRaisesMessage(ValidationError, "已发布"):
            self.update_draft(draft, payload)

    def test_auxiliary_recursive_shape_cannot_hide_behind_published_status(self):
        self.generated_field_fixture()
        draft = self.managed_draft()
        leaf = self.leaf_fixture()
        ExecutionAuxiliaryBinding.objects.create(version=leaf, purpose="generate_fields", target_version=draft)
        with self.assertRaisesMessage(ValidationError, "叶子"):
            self.update_draft(draft, {"auxiliaries": [{"purpose": "generate_fields", "target_version_id": str(leaf.pk)}]})

    def mapping_fixture(self):
        version = self.version()
        version.save()
        first = self.input_spec(version)
        first.save()
        second = self.input_spec(version, key="other_images", position=1)
        second.save()
        for spec in (first, second):
            ExecutionInputTarget.objects.create(
                input_spec=spec, target_kind="workflow_slot", target_key=spec.key, is_primary=True,
            )
        return version, first, second

    def test_mapping_keeps_logical_inputs_and_shared_profile_context(self):
        version, first, _ = self.mapping_fixture()
        ExecutionInputTarget.objects.create(
            input_spec=first, target_kind="profile_input", target_key="reference_context", is_primary=False,
        )
        validate_execution_input_mapping(version.inputs.prefetch_related("targets"))

    def test_different_inputs_cannot_claim_the_same_physical_slot(self):
        version, first, second = self.mapping_fixture()
        second.targets.update(target_key=first.key)
        with self.assertRaisesMessage(ValidationError, "同一个执行槽位"):
            validate_execution_input_mapping(version.inputs.prefetch_related("targets"))

    def test_missing_primary_or_all_targets_is_not_silently_filled(self):
        version, first, _ = self.mapping_fixture()
        first.targets.update(is_primary=False)
        with self.assertRaisesMessage(ValidationError, "主执行槽位"):
            validate_execution_input_mapping(version.inputs.prefetch_related("targets"))
        first.targets.all().delete()
        with self.assertRaisesMessage(ValidationError, "未配置目标输入"):
            validate_execution_input_mapping(version.inputs.prefetch_related("targets"))

    def test_profile_only_input_does_not_invent_an_execution_slot(self):
        version, first, _ = self.mapping_fixture()
        first.targets.update(target_kind="profile_input", is_primary=False)
        validate_execution_input_mapping(version.inputs.prefetch_related("targets"))

    def test_pairing_must_resolve_without_cycles(self):
        version, first, second = self.mapping_fixture()
        for key in ("missing_input", first.key):
            first.semantic_contract["paired_with_input_key"] = key
            first.save()
            with self.assertRaises(ValidationError):
                validate_execution_input_mapping(version.inputs.prefetch_related("targets"))
        first.semantic_contract["paired_with_input_key"] = second.key
        first.save()
        validate_execution_input_mapping(version.inputs.prefetch_related("targets"))
        second.semantic_contract["paired_with_input_key"] = first.key
        second.save()
        with self.assertRaisesMessage(ValidationError, "循环"):
            validate_execution_input_mapping(version.inputs.prefetch_related("targets"))

    def test_parameter_database_policy_union(self):
        version = self.version()
        version.save()
        for fields in (
            {"policy": "locked", "locked_value": None},
            {"policy": "locked", "locked_value": 1, "default_value": 1},
            {"policy": "overridable", "locked_value": 1},
        ):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                ExecutionParameterSpec.objects.create(version=version, key="sample", value_type="integer", **fields)

    def test_parameter_database_seed_union(self):
        version = self.version()
        version.save()
        for fields in (
            {"seed_policy": "unknown"}, {"seed_policy": ""},
            {"seed_policy": "fixed", "value_type": "number"},
            {"seed_policy": "generate_once", "default_value": 5},
            {"seed_policy": "generate_once", "policy": "locked", "locked_value": 5},
        ):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                ExecutionParameterSpec.objects.create(version=version, key="sample", **{
                    "value_type": "integer", "policy": "overridable", **fields,
                })

    def test_parameter_seed_setting_is_saved_cloned_and_audited(self):
        self.workflow_parameter_fixture()
        draft = self.managed_draft()
        self.update_draft(draft, {"parameters": [self.parameter_payload(default_value=None, seed_policy="generate_once")]})
        draft.refresh_from_db()
        self.assertEqual(draft.parameters.get().seed_policy, "generate_once")
        with patch("studio.services.execution_instances.verify_workflow_version_integrity"):
            clone = clone_execution_draft(source_id=draft.pk, expected_revision=draft.revision, actor=self.actor)
        self.assertEqual(clone.parameters.get().seed_policy, "generate_once")

    def test_contract_size_hash_and_one_to_one(self):
        version = self.version()
        version.save()
        contract = json_contract({"schema_version": 1, "execution": {"operation": version.operation}})
        row = ExecutionVersionContract(
            version=version, definition=contract.value, schema_version=1,
            size_bytes=contract.size_bytes, sha256=contract.sha256, target_fingerprint="a" * 64,
        )
        row.full_clean()
        row.save()
        row.definition["tampered"] = True
        with self.assertRaises(ValidationError):
            row.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExecutionVersionContract.objects.create(
                version=version, definition=contract.value, schema_version=1,
                size_bytes=contract.size_bytes, sha256=contract.sha256, target_fingerprint="a" * 64,
            )

    def selection(self):
        return {
            "deployment": self.deployment, "operation": "video.reference_to_video",
            "mode": "Ref2VA", "prompt_mode": "profile",
            "prompt_profile_version": self.profile, "workflow_version": self.workflow,
        }

    def managed_draft(self):
        with patch("studio.services.execution_instances.verify_workflow_version_integrity"):
            return create_execution_draft(
                instance_id=self.instance.pk, expected_revision=1, selection=self.selection(), actor=self.actor,
            )

    def update_draft(self, version, changes, *, expected_revision=1):
        with patch("studio.services.execution_instances.verify_workflow_version_integrity"):
            return update_execution_draft(
                version_id=version.pk, expected_revision=expected_revision, changes=changes, actor=self.actor,
            )

    def test_registration_writes_existing_lifecycle_audit(self):
        instance = create_execution_instance(key="created-instance", name="Created", description="", actor=self.actor)
        event = PlatformLifecycleAuditEvent.objects.get(object_id=instance.pk)
        self.assertEqual(event.actor_id, self.actor.pk)
        self.assertEqual(event.after_summary, {"key": "created-instance", "is_active": True, "revision": 1})

    def test_draft_version_allocation_uses_parent_revision(self):
        first = self.managed_draft()
        self.assertEqual(first.version, 1)
        self.assertEqual(first.status, "draft")
        self.assertFalse(ExecutionVersionContract.objects.filter(version=first).exists())
        with self.assertRaises(ValidationError) as caught:
            self.managed_draft()
        self.assertEqual(caught.exception.code, "execution_definition_conflict")
        self.assertEqual(ModelExecutionInstanceVersion.objects.count(), 1)
        with patch("studio.services.execution_instances.verify_workflow_version_integrity"):
            second = create_execution_draft(
                instance_id=self.instance.pk, expected_revision=2, selection=self.selection(), actor=self.actor,
            )
        self.assertEqual(second.version, 2)

    def test_draft_changes_are_atomic_and_require_fresh_revision(self):
        draft = self.managed_draft()
        updated = self.update_draft(draft, {"prompt_mode": "manual", "prompt_profile_version": None})
        self.assertEqual(updated.revision, 2)
        self.assertIsNone(updated.prompt_profile_version_id)
        with self.assertRaises(ValidationError):
            self.update_draft(draft, {"mode": ""})
        updated.refresh_from_db()
        self.assertEqual(updated.mode, "Ref2VA")
        self.assertEqual(updated.revision, 2)

    def test_clone_copies_choices_but_not_ids_publication_or_contract(self):
        from studio.services.execution_instances import execution_version_configuration

        draft = self.managed_draft()
        draft = self.update_draft(draft, {"inputs": [self.input_payload()]})
        source_ids = set(draft.inputs.values_list("pk", flat=True))
        with patch("studio.services.execution_instances.verify_workflow_version_integrity"):
            clone = clone_execution_draft(source_id=draft.pk, expected_revision=2, actor=self.actor)
        self.assertEqual(execution_version_configuration(draft), execution_version_configuration(clone))
        self.assertEqual(clone.version, 2)
        self.assertEqual(clone.status, "draft")
        self.assertIsNone(clone.published_at)
        self.assertIsNone(clone.published_by_id)
        self.assertFalse(clone.inputs.filter(pk__in=source_ids).exists())
        self.assertFalse(ExecutionVersionContract.objects.filter(version=clone).exists())

    def test_clone_failure_rolls_back_version_allocation_and_audit(self):
        draft = self.managed_draft()
        draft = self.update_draft(draft, {"inputs": [self.input_payload()]})
        draft.inputs.update(max_items=4)
        self.instance.refresh_from_db()
        parent_revision = self.instance.revision
        audit_count = PlatformLifecycleAuditEvent.objects.count()
        with patch("studio.services.execution_instances.verify_workflow_version_integrity"), self.assertRaises(ValidationError):
            clone_execution_draft(source_id=draft.pk, expected_revision=2, actor=self.actor)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.revision, parent_revision)
        self.assertEqual(self.instance.versions.count(), 1)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), audit_count)

    def test_clone_requires_the_reviewed_source_revision(self):
        draft = self.managed_draft()
        with self.assertRaises(ValidationError):
            clone_execution_draft(source_id=draft.pk, expected_revision=2, actor=self.actor)
        self.assertEqual(self.instance.versions.count(), 1)

    def test_child_configuration_changes_are_present_in_audit_digest(self):
        draft = self.managed_draft()
        self.update_draft(draft, {"inputs": [self.input_payload()]})
        event = PlatformLifecycleAuditEvent.objects.filter(object_id=draft.pk, action="update").latest("created_at")
        self.assertNotEqual(event.before_summary["sha256"], event.after_summary["sha256"])
        self.assertNotIn("inputs", event.after_summary)

    def test_published_draft_writer_refuses_content_changes(self):
        from django.utils import timezone

        draft = self.managed_draft()
        ModelExecutionInstanceVersion.objects.filter(pk=draft.pk).update(
            status="published", published_at=timezone.now(), published_by=self.actor,
        )
        with self.assertRaises(ValidationError):
            self.update_draft(draft, {"prompt_mode": "manual", "prompt_profile_version": None})
        draft.refresh_from_db()
        self.assertEqual(draft.prompt_mode, "profile")

    def test_lifecycle_and_derived_fields_cannot_be_patched(self):
        draft = self.managed_draft()
        for changes in ({"status": "published"}, {"version": 8}, {"contract": {}}, {"revision": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.update_draft(draft, changes)
        for expected in (True, None, 0, "1"):
            with self.subTest(expected=expected), self.assertRaises(ValidationError):
                self.update_draft(draft, {"mode": "Ref2VA"}, expected_revision=expected)

    def input_payload(self):
        return {
            "key": "reference_images", "position": 0, "kind": "image", "required": True,
            "min_items": 1, "max_items": 3, "semantic_contract": semantic_contract(),
            "targets": [{"target_kind": "workflow_slot", "target_key": "images", "is_primary": True}],
        }

    def test_input_replace_failure_restores_children_revision_and_audit(self):
        draft = self.managed_draft()
        draft = self.update_draft(draft, {"inputs": [self.input_payload()]})
        original_id = draft.inputs.get().pk
        audit_count = PlatformLifecycleAuditEvent.objects.filter(object_id=draft.pk).count()
        invalid = self.input_payload()
        invalid["min_items"] = 5
        with self.assertRaises(ValidationError):
            self.update_draft(draft, {"inputs": [invalid]}, expected_revision=2)
        draft.refresh_from_db()
        self.assertEqual(draft.revision, 2)
        self.assertEqual(draft.inputs.get().pk, original_id)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.filter(object_id=draft.pk).count(), audit_count)

    def test_input_target_type_range_and_existence_checked_before_save(self):
        draft = self.managed_draft()
        for change in ({"kind": "video"}, {"max_items": 4}, {"targets": [{"target_kind": "workflow_slot", "target_key": "missing", "is_primary": True}]},
                       {"targets": [{"target_kind": "operation_input", "target_key": "images", "is_primary": True}]}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.update_draft(draft, {"inputs": [{**self.input_payload(), **change}]})
        draft.refresh_from_db()
        self.assertEqual(draft.revision, 1)
        self.assertFalse(draft.inputs.exists())

    def test_input_target_rechecked_when_changing_workflow(self):
        draft = self.managed_draft()
        draft = self.update_draft(draft, {"inputs": [self.input_payload()]})
        other = WorkflowTemplateVersion.objects.create(
            template=self.template, version=2, provider=self.provider,
            model_profile_version=self.model, validated_deployment=self.deployment,
            status="published", input_contract={**self.workflow.input_contract, "classified_references": None},
        )
        with self.assertRaisesMessage(ValidationError, "未声明目标"):
            self.update_draft(draft, {"workflow_version": other}, expected_revision=2)
        draft.refresh_from_db()
        self.assertEqual(draft.workflow_version_id, self.workflow.pk)
        self.assertEqual(draft.revision, 2)

    def test_required_target_coverage_is_checked_for_complete_definitions(self):
        from studio.services.execution_inputs import validate_workflow_input_targets

        self.workflow.input_contract["classified_references"]["groups"]["images"]["min_items"] = 1
        WorkflowTemplateVersion.objects.filter(pk=self.workflow.pk).update(input_contract=self.workflow.input_contract)
        draft = self.managed_draft()
        validate_workflow_input_targets(draft, require_complete=False)
        with self.assertRaisesMessage(ValidationError, "尚未接线"):
            validate_workflow_input_targets(draft, require_complete=True)
        draft = self.update_draft(draft, {"inputs": [self.input_payload()]})
        validate_workflow_input_targets(draft, require_complete=True)
        with self.assertRaises(ValidationError):
            self.update_draft(draft, {"inputs": [{**self.input_payload(), "required": False, "min_items": 0}]}, expected_revision=2)

    def test_parameter_failure_rolls_back_prior_selection_changes(self):
        draft = self.managed_draft()
        parameter = {
            "key": "sample", "value_type": "integer", "minimum": 1, "maximum": 10,
            "enum_values": [], "step": 1, "remainder": None, "policy": "overridable",
            "default_value": True, "locked_value": None, "seed_policy": None,
        }
        with self.assertRaises(ValidationError):
            self.update_draft(draft, {
                "prompt_mode": "manual", "prompt_profile_version": None, "parameters": [parameter],
            })
        draft.refresh_from_db()
        self.assertEqual(draft.prompt_mode, "profile")
        self.assertFalse(draft.parameters.exists())

    def test_create_draft_validation_failure_does_not_consume_version(self):
        invalid = self.selection()
        invalid["workflow_version"] = None
        with self.assertRaises(ValidationError):
            create_execution_draft(instance_id=self.instance.pk, expected_revision=1, selection=invalid, actor=self.actor)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.revision, 1)
        self.assertEqual(self.managed_draft().version, 1)

    def parameter_payload(self, **changes):
        payload = {
            "key": "sampling.value", "value_type": "integer", "minimum": 0, "maximum": 100,
            "enum_values": [], "step": 6, "remainder": 3, "policy": "overridable",
            "default_value": 9, "locked_value": None, "seed_policy": None,
        }
        payload.update(changes)
        return payload

    def workflow_parameter_fixture(self, **changes):
        self.workflow.input_contract["parameters"] = {"sampling.value": {
            "type": "integer", "required": True, "min": 0, "max": 100,
            "alignment_modulus": 4, "alignment_remainder": 1, "default": 9, **changes,
        }}
        # Deliberately isolated fixture; production integrity remains mocked by
        # the draft helper, not disabled in the service.
        WorkflowTemplateVersion.objects.filter(pk=self.workflow.pk).update(input_contract=self.workflow.input_contract)
        WorkflowSemanticBinding.objects.update_or_create(
            workflow_version=self.workflow, node_id="fixture-node", input_name="value",
            defaults={"semantic_key": "sampling.value", "value_type": "integer", "is_required": True},
        )

    def test_workflow_parameter_save_validates_combined_alignment(self):
        self.workflow_parameter_fixture()
        draft = self.managed_draft()
        draft = self.update_draft(draft, {"parameters": [self.parameter_payload()]})
        original = draft.parameters.get().pk
        # 3 satisfies the instance layer but not the Workflow layer.
        with self.assertRaises(ValidationError):
            self.update_draft(draft, {"parameters": [self.parameter_payload(default_value=3)]}, expected_revision=2)
        draft.refresh_from_db()
        self.assertEqual(draft.revision, 2)
        self.assertEqual(draft.parameters.get().pk, original)

    def test_workflow_parameter_range_cannot_expand_or_add_unknown_input(self):
        self.workflow_parameter_fixture()
        draft = self.managed_draft()
        for changes in ({"minimum": -1}, {"maximum": 101}, {"key": "missing.value"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.update_draft(draft, {"parameters": [self.parameter_payload(**changes)]})
        self.assertFalse(draft.parameters.exists())

    def test_workflow_fixed_value_cannot_be_overwritten_by_instance(self):
        self.workflow_parameter_fixture(const=9)
        draft = self.managed_draft()
        with self.assertRaisesMessage(ValidationError, "固定值冲突"):
            self.update_draft(draft, {"parameters": [self.parameter_payload(
                policy="locked", default_value=None, locked_value=21,
            )]})
        self.assertFalse(draft.parameters.exists())

    def test_workflow_selection_change_rechecks_existing_parameter_rows(self):
        self.workflow_parameter_fixture()
        draft = self.managed_draft()
        draft = self.update_draft(draft, {"parameters": [self.parameter_payload()]})
        new_contract = deepcopy(self.workflow.input_contract)
        new_contract["parameters"]["sampling.value"]["max"] = 30
        alternate = WorkflowTemplateVersion.objects.create(
            template=self.template, version=2, provider=self.provider,
            model_profile_version=self.model, validated_deployment=self.deployment,
            status="published", input_contract=new_contract,
        )
        with self.assertRaises(ValidationError):
            self.update_draft(draft, {"workflow_version": alternate}, expected_revision=2)
        draft.refresh_from_db()
        self.assertEqual(draft.workflow_version_id, self.workflow.pk)
        self.assertEqual(draft.revision, 2)

    def test_configured_storage_limit_blocks_contract_reads_too(self):
        draft = self.managed_draft()
        value = json_contract({"content": "x" * 200})
        row = ExecutionVersionContract(
            version=draft, definition=value.value, schema_version=1,
            size_bytes=value.size_bytes, sha256=value.sha256, target_fingerprint="a" * 64,
        )
        row.full_clean()
        with patch("studio.services.execution_instances.boundary_int", return_value=100):
            with self.assertRaises(ValidationError):
                row.full_clean()
