from copy import deepcopy

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    PlatformLifecycleAuditEvent,
    WorkflowProjector,
    WorkflowProjectorVersion,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.workflow_projectors import (
    WorkflowProjectorAmbiguous,
    WorkflowProjectorError,
    current_projector_catalog,
    select_workflow_projector,
    validate_frozen_workflow_projector_contract,
    validate_workflow_projector_definition,
    validate_workflow_projector_publish_conflicts,
    workflow_projector_configuration_options,
    workflow_projector_engine_options,
    workflow_projector_creation_template,
)


class WorkflowProjectorContractTests(TestCase):
    def test_missing_projector_cannot_use_a_compiler_as_an_execution_bypass(self):
        for compiler in ({"id": "generic.comfy", "version": 1}, {"id": "generic.comfy", "version": 2}, {"id": "minimax_h3", "version": 3}):
            with self.subTest(compiler=compiler), self.assertRaisesRegex(WorkflowProjectorError, "缺少冻结的 Projector"):
                validate_frozen_workflow_projector_contract({"compiler": compiler})

    def test_blank_creation_templates_have_no_registry_or_business_dependencies(self):
        with self.assertNumQueries(0):
            engines = workflow_projector_engine_options()
        for engine in engines:
            template = engine['creation_template']
            definition = template['definition']
            with self.subTest(engine=engine['key']):
                self.assertEqual(template['output_kind'], '')
                self.assertEqual(definition['adapters'], engine['adapter_contract'])
                self.assertEqual(definition['projection']['kind'], engine['projection_kind'])
                for key in ('output_nodes', 'model_selectors', 'node_roles', 'required_edges', 'parameter_policies', 'asset_slots'):
                    self.assertFalse(definition[key])
                self.assertFalse(definition['detector']['node_counts'])
                self.assertFalse(definition['detector']['fallback'])
                with self.assertRaises(WorkflowProjectorError):
                    validate_workflow_projector_definition(definition, engine_key=engine['key'], engine_version=engine['version'])
        engines[0]['creation_template']['definition']['output_nodes']['Test'] = 'image'
        self.assertEqual(workflow_projector_engine_options()[0]['creation_template']['definition']['output_nodes'], {})
        with self.assertRaises(WorkflowProjectorError):
            workflow_projector_creation_template('not.registered')

    def _generic(self):
        return next(
            row for row in current_projector_catalog()
            if row.key == "generic.comfy.fixed"
        )

    def test_migration_seeds_versioned_current_projector_catalog(self):
        catalog = current_projector_catalog()

        self.assertEqual(
            {row.key for row in catalog},
            {
                "generic.comfy.fixed",
                "minimax_h3.dual_clock_t2va",
                "minimax_h3.dual_clock_fl2va",
                "minimax_h3.classified_ref2va",
            },
        )
        self.assertTrue(all(row.version == 1 for row in catalog))
        self.assertTrue(all(len(row.definition_sha256) == 64 for row in catalog))

    def test_editor_catalog_is_engine_owned_and_covers_definition_sections(self):
        engines = workflow_projector_engine_options()
        configuration = workflow_projector_configuration_options()

        self.assertEqual(configuration["editor_schema_version"], 1)
        self.assertEqual(
            {row["key"] for row in engines},
            {"generic.fixed", "generic.references", "generic.classified"},
        )
        for engine in engines:
            with self.subTest(engine=engine["key"]):
                if engine["key"] == "generic.references":
                    self.assertEqual(engine["adapter_contract"]["compiler"], "generic.comfy/v2")
                    continue
                self.assertEqual(
                    engine["adapter_contract"],
                    next(
                        row.definition["adapters"]
                        for row in current_projector_catalog()
                        if row.engine_key == engine["key"]
                    ),
                )
                self.assertIn("adapters", engine["locked_fields"])
                self.assertTrue(engine["projection_kind"])

        editor = configuration["definition_editor"]
        self.assertEqual(
            [section["key"] for section in editor["sections"]],
            ["matching", "outputs", "graph", "inputs", "execution", "advanced"],
        )
        field_paths = {
            field["path"]
            for section in editor["sections"]
            for field in section["fields"]
        }
        self.assertEqual(
            field_paths,
            {
                "schema_version", "priority", "detector.fallback",
                "detector.allow_other_nodes", "detector.node_counts",
                "detector.optional_node_counts", "detector.input_equals",
                "output_nodes", "model_selectors", "node_roles",
                "required_edges", "parameter_policies", "asset_slots",
                "adapters", "projection", "$",
            },
        )
        self.assertEqual(
            {row["value"] for row in editor["choices"]["parameter_policies"]},
            {"fixed", "object_info", "source_locked", "runtime"},
        )
        self.assertEqual(editor["limits"]["priority_max"], 1000)

    def test_generic_selection_freezes_exact_version_and_retirement_keeps_history_valid(self):
        selected = select_workflow_projector({
            "1": {"class_type": "SaveImage", "inputs": {}},
        })
        frozen = {
            "contract_projector": selected.identity,
            "workflow_adapters": deepcopy(selected.definition["adapters"]),
            "compiler": {"id": "generic.comfy", "version": 1},
        }

        validated = validate_frozen_workflow_projector_contract(frozen)
        self.assertEqual(validated.version_id, selected.version_id)
        self.assertEqual(validated.definition_sha256, selected.definition_sha256)

        version = WorkflowProjectorVersion.objects.get(pk=selected.version_id)
        version.is_current = False
        version.status = WorkflowProjectorVersion.Status.RETIRED
        version.save(update_fields=("is_current", "status", "updated_at"))

        historical = validate_frozen_workflow_projector_contract(frozen)
        self.assertEqual(historical.version_id, selected.version_id)
        self.assertEqual(historical.definition_sha256, selected.definition_sha256)

    def test_frozen_validation_accepts_successor_or_alternate_family_on_same_engine(self):
        base = self._generic()
        base_version = WorkflowProjectorVersion.objects.select_related(
            "projector",
        ).get(pk=base.version_id)

        def publish(projector, version):
            row = WorkflowProjectorVersion(
                projector=projector,
                version=version,
                provider_driver_type=base.provider_driver_type,
                operation=base.operation,
                output_kind=base.output_kind,
                engine_key=base.engine_key,
                engine_version=base.engine_version,
                status=WorkflowProjectorVersion.Status.PUBLISHED,
                is_current=False,
                published_at=timezone.now(),
            )
            assign_json_contract(row, "definition", deepcopy(base.definition))
            row.save()
            return row

        successor = publish(base_version.projector, base.version + 1)
        alternate_family = WorkflowProjector.objects.create(
            key="test.generic.compatible",
            name="Compatible generic projector",
        )
        alternate = publish(alternate_family, 1)

        for row in (successor, alternate):
            with self.subTest(projector=row.projector.key, version=row.version):
                frozen = {
                    "contract_projector": {
                        "id": row.projector.key,
                        "version": row.version,
                        "version_id": str(row.id),
                        "definition_sha256": row.definition_sha256,
                        "provider_driver_type": row.provider_driver_type,
                        "operation": row.operation,
                        "output_kind": row.output_kind,
                        "engine": {
                            "key": row.engine_key,
                            "version": row.engine_version,
                        },
                    },
                    "workflow_adapters": deepcopy(row.definition["adapters"]),
                    "compiler": {"id": "generic.comfy", "version": 1},
                }
                validated = validate_frozen_workflow_projector_contract(frozen)
                self.assertEqual(validated.version_id, str(row.id))
                self.assertEqual(validated.engine_key, base.engine_key)

    def test_family_key_and_published_version_content_are_immutable(self):
        base = self._generic()
        family = WorkflowProjector.objects.get(key=base.key)
        family.key = "generic.comfy.renamed"
        with self.assertRaises(ValidationError):
            family.save(update_fields=("key", "updated_at"))
        with self.assertRaises(ValidationError):
            WorkflowProjector.objects.filter(pk=family.pk).update(
                key="generic.comfy.renamed",
            )

        version = WorkflowProjectorVersion.objects.get(pk=base.version_id)
        version.engine_version += 1
        with self.assertRaises(ValidationError):
            version.save(update_fields=("engine_version", "updated_at"))
        with self.assertRaises(ValidationError):
            WorkflowProjectorVersion.objects.filter(pk=version.pk).update(
                definition={},
            )
        with self.assertRaises(ValidationError):
            version.delete()

    def test_publish_conflict_rejects_equal_priority_overlapping_detector(self):
        generic = self._generic()
        family = WorkflowProjector.objects.create(
            key="test.generic.conflict",
            name="Conflicting generic projector",
        )
        draft = WorkflowProjectorVersion(
            projector=family,
            version=1,
            provider_driver_type=generic.provider_driver_type,
            operation=generic.operation,
            output_kind=generic.output_kind,
            engine_key=generic.engine_key,
            engine_version=generic.engine_version,
        )
        assign_json_contract(draft, "definition", deepcopy(generic.definition))
        draft.save()

        with self.assertRaises(WorkflowProjectorAmbiguous):
            validate_workflow_projector_publish_conflicts(draft)

    def test_definition_rejects_unregistered_adapter_and_secret_field(self):
        generic = self._generic()
        unregistered = deepcopy(generic.definition)
        unregistered["adapters"]["compiler"] = "private.compiler/v1"
        with self.assertRaises(ValidationError):
            validate_workflow_projector_definition(
                unregistered,
                engine_key=generic.engine_key,
                engine_version=generic.engine_version,
            )

        secret = deepcopy(generic.definition)
        secret["projection"]["config"] = {"api_token": "must-not-persist"}
        with self.assertRaises(ValidationError):
            validate_workflow_projector_definition(
                secret,
                engine_key=generic.engine_key,
                engine_version=generic.engine_version,
            )


class WorkflowProjectorLifecycleApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="projector-lifecycle-admin",
            password="StrongPass123!",
            email="projector-lifecycle@example.test",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.current = WorkflowProjectorVersion.objects.select_related(
            "projector",
        ).get(
            projector__key="generic.comfy.fixed",
            is_current=True,
        )

    def test_options_and_creation_support_a_new_family_without_a_seed_version(self):
        options = self.client.get('/api/v1/workflow-projector-versions/options/')
        self.assertEqual(options.status_code, 200, options.data)
        engine = next(row for row in options.data['engines'] if row['key'] == 'generic.fixed')
        template = deepcopy(engine['creation_template'])
        family = WorkflowProjector.objects.create(key='blank.image', name='Blank image')
        payload = {**template, 'engine_key': engine['key'], 'engine_version': engine['version'], 'projector': str(family.pk)}
        invalid = self.client.post('/api/v1/workflow-projector-versions/', payload, format='json')
        self.assertEqual(invalid.status_code, 400, invalid.data)
        self.assertFalse(family.versions.exists())
        template['definition']['output_nodes'] = {'SaveImage': 'image'}
        template['definition']['detector']['node_counts'] = {'SaveImage': 1}
        created = self.client.post('/api/v1/workflow-projector-versions/', payload, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data['status'], 'draft')
        self.assertFalse(created.data['is_current'])
        self.assertEqual(created.data['version'], 1)
        self.assertEqual(created.data['definition'], template['definition'])
        self.assertFalse(created.data['definition']['model_selectors'])
        self.current.refresh_from_db()
        self.assertTrue(self.current.is_current)
        self.assertEqual(self.current.status, WorkflowProjectorVersion.Status.PUBLISHED)

    def test_latest_unreferenced_successor_draft_can_be_discarded_with_audit(self):
        created = self.client.post(
            f"/api/v1/workflow-projector-versions/{self.current.id}/draft-next/",
            {},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        draft_id = created.data["id"]

        discarded = self.client.post(
            f"/api/v1/workflow-projector-versions/{draft_id}/discard/",
            {},
            format="json",
            HTTP_X_AUDIT_REASON_CODE="lifecycle_cleanup",
        )

        self.assertEqual(discarded.status_code, 204, discarded.data)
        self.assertFalse(WorkflowProjectorVersion.objects.filter(pk=draft_id).exists())
        self.current.refresh_from_db()
        self.assertEqual(
            self.current.status,
            WorkflowProjectorVersion.Status.PUBLISHED,
        )
        self.assertTrue(self.current.is_current)
        event = PlatformLifecycleAuditEvent.objects.get(
            object_type="workflow_projector_version",
            object_id=draft_id,
            action="discard",
        )
        self.assertEqual(event.reason_code, "lifecycle_cleanup")
        self.assertEqual(event.before_summary["status"], "draft")
        self.assertEqual(event.after_summary, {})

    def test_resolution_policy_api_roundtrip_and_published_source_unchanged(self):
        from studio.services.workflow_resolution import CONFIGURED_RESOLUTION_ADAPTER
        original_sha = self.current.definition_sha256
        options = self.client.get('/api/v1/workflow-projector-versions/options/')
        self.assertEqual(options.status_code, 200, options.data)
        self.assertEqual(options.data['image_resolution']['configured_adapter'], CONFIGURED_RESOLUTION_ADAPTER)
        created = self.client.post(f'/api/v1/workflow-projector-versions/{self.current.id}/draft-next/', {}, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        definition = deepcopy(created.data['definition'])
        policy = {'adapter':CONFIGURED_RESOLUTION_ADAPTER,'sizes':[{'width':768,'height':512}]}
        definition['projection']['config']['image_resolution'] = policy
        url = f"/api/v1/workflow-projector-versions/{created.data['id']}/"
        saved = self.client.patch(url, {'definition':definition}, format='json')
        self.assertEqual(saved.status_code, 200, saved.data)
        detail = self.client.get(url)
        self.assertEqual(detail.data['definition']['projection']['config']['image_resolution'], policy)
        self.current.refresh_from_db()
        self.assertEqual(self.current.definition_sha256, original_sha)
        self.assertEqual(self.current.definition['projection']['config'], {})
        definition['projection']['config']['image_resolution']['sizes'][0]['width'] = 769
        rejected = self.client.patch(url, {'definition':definition}, format='json')
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(self.client.get(url).data['definition']['projection']['config']['image_resolution']['sizes'][0]['width'],768)
        frozen = {'contract_projector':self._frozen_identity(), 'workflow_adapters':deepcopy(self.current.definition['adapters']),
                  'compiler':{'id':'generic.comfy','version':1}, 'resolution_strategy':policy}
        with self.assertRaises(WorkflowProjectorError):
            validate_frozen_workflow_projector_contract(frozen)

    def _frozen_identity(self):
        return next(row.identity for row in current_projector_catalog() if row.version_id == str(self.current.id))

    def test_published_projector_version_cannot_be_discarded(self):
        response = self.client.post(
            f"/api/v1/workflow-projector-versions/{self.current.id}/discard/",
            {},
            format="json",
            HTTP_X_AUDIT_REASON_CODE="lifecycle_cleanup",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertTrue(
            WorkflowProjectorVersion.objects.filter(pk=self.current.id).exists(),
        )
        self.assertFalse(PlatformLifecycleAuditEvent.objects.filter(
            object_type="workflow_projector_version",
            object_id=self.current.id,
            action="discard",
        ).exists())
