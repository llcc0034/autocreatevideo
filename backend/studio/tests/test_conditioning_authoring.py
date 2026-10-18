from copy import deepcopy
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import ModelProfileVersion, PlatformLifecycleAuditEvent, ProviderProfile
from studio.platform_serializers import ModelProfileRegistryOptionsSerializer
from studio.services.json_contracts import assign_json_contract
from studio.services.media_capabilities import normalize_conditioning_contract, reference_topology_editor_options
from studio.services.model_profile_options import model_profile_registry_options
from studio.services.reference_media import REFERENCE_PREPROCESSING_ADAPTERS
from studio.tests.factories import conditioning_contract


class ConditioningAuthoringTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_superuser(username='conditioning-author', password='IsolatedTestOnly123!'))
        self.provider = ProviderProfile(key='conditioning-author', name='Isolated', driver_type='comfyui', modalities='video')
        assign_json_contract(self.provider, 'driver_config', {})
        self.provider.save()
        self.url = '/api/v1/model-profile-versions/'
        self.spec = {'schema_version': 1, 'capabilities': ['video.reference_to_video'], 'conditioning_contract': conditioning_contract(
            'References', input_kind='classified_references', reference_videos=(0, 3), paired_video_audios=(0, 2), reference_audios=(0, 2))}

    def test_catalog_is_runtime_registry_and_malformed_refs_are_validation_errors(self):
        options = reference_topology_editor_options()
        self.assertEqual(ModelProfileRegistryOptionsSerializer(model_profile_registry_options()).data['conditioning_contract']['dynamic_reference_topology'], options)
        for slot in options['preprocessing_slots']:
            self.assertEqual({row['value'] for row in slot['adapters']}, REFERENCE_PREPROCESSING_ADAPTERS[slot['value']])
        topology = self.spec['conditioning_contract']['modes'][0]['dynamic_reference_topology']
        for field, key, values in [('pairing_rules', 'source_purpose_keys', [{}]), ('pairing_rules', 'target_purpose_keys', [[]]), ('mutual_exclusion_rules', 'purpose_keys', [{}, []])]:
            bad = deepcopy(self.spec['conditioning_contract'])
            bad['modes'][0]['dynamic_reference_topology'][field][0][key] = values
            with self.subTest(field=field, key=key), self.assertRaises(ValidationError):
                normalize_conditioning_contract(bad)
        self.assertTrue(topology['videos']['purposes'])

    def test_authoring_roundtrip_published_immutability_and_invalid_write_no_audit(self):
        original = deepcopy(self.spec)
        payload = {'profile_key': 'conditioning-author', 'model_identifier': 'isolated-model', 'modality': 'video', 'provider': str(self.provider.pk), 'specification': original}
        response = self.client.post(self.url, payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        detail = f"{self.url}{response.data['id']}/"
        changed = deepcopy(original)
        topology = changed['conditioning_contract']['modes'][0]['dynamic_reference_topology']
        topology['videos']['purposes'][0]['label'] = '镜头运动参考'
        topology['preprocessing']['videos']['target_fps'] = 30
        topology['pairing_rules'][0]['allow_embedded_source'] = False
        response = self.client.patch(detail, {'specification': changed}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['specification'], changed)
        bad = deepcopy(changed)
        bad['conditioning_contract']['modes'][0]['dynamic_reference_topology']['pairing_rules'][0]['source_purpose_keys'] = [{}]
        counts = ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()
        response = self.client.patch(detail, {'specification': bad}, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual((ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()), counts)
        self.assertEqual(self.client.get(detail).data['specification'], changed)
        response = self.client.post(f'{detail}publish/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        sha = self.client.get(detail).data['specification_sha256']
        self.assertEqual(self.client.patch(detail, {'specification': original}, format='json').status_code, 400)
        counts = ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()
        response = self.client.post(f'{detail}draft-next/', {'specification': bad}, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual((ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()), counts)
        response = self.client.post(f'{detail}draft-next/', {'specification': original}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['specification'], original)
        self.assertEqual(self.client.get(detail).data['specification_sha256'], sha)
