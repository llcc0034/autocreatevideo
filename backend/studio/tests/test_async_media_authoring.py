from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ModelProfileVersion, PlatformLifecycleAuditEvent, ProviderProfile
from studio.platform_serializers import ModelProfileRegistryOptionsSerializer
from studio.services.async_media_contract import validate_async_media_specification, validate_standard_payload
from studio.services.async_media_schema import STANDARD_MEDIA_FIELDS, async_media_editor_options
from studio.services.json_contracts import assign_json_contract
from studio.services.model_profile_options import model_profile_registry_options
from studio.services.provider_registry import RestrictedAsyncRestDriver
from studio.tests.test_async_media_p0 import _async_spec, _provider_config


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=('api.example.com', 'cdn.example.com'))
class AsyncMediaAuthoringTests(TestCase):
    def setUp(self):
        self.enterContext(patch('studio.services.provider_registry._request_json', side_effect=AssertionError('No network')))
        self.enterContext(patch('studio.services.provider_registry.resolve_credential', side_effect=AssertionError('No credentials')))
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_superuser(username='async-authoring', password='IsolatedTestOnly123!'))
        self.provider = ProviderProfile(key='async-authoring', name='Isolated', driver_type='async_rest', modalities='image,video')
        assign_json_contract(self.provider, 'driver_config', _provider_config())
        self.provider.save()
        self.url = '/api/v1/model-profile-versions/'

    def payload(self, spec):
        return {'profile_key': 'async-authoring', 'model_identifier': 'cloud-v1', 'modality': 'video', 'provider': str(self.provider.pk), 'specification': spec}

    def test_catalog_and_invalid_rule_matrix_use_runtime_validator(self):
        catalog = async_media_editor_options()
        self.assertEqual(ModelProfileRegistryOptionsSerializer(model_profile_registry_options()).data['async_media'], catalog)
        self.assertEqual({row['key'] for row in catalog['fields']}, STANDARD_MEDIA_FIELDS)
        self.assertEqual(RestrictedAsyncRestDriver.STANDARD_REQUEST_FIELDS, STANDARD_MEDIA_FIELDS)
        original = _async_spec()
        self.assertEqual(validate_async_media_specification(original, required=True), ['image.text_to_image', 'video.text_to_video'])
        invalid_rules = [
            {'type': 'integer', 'minimum': True}, {'type': 'integer', 'maximum': '100'},
            {'type': 'integer', 'minimum': 2, 'maximum': 1}, {'type': 'integer', 'minimum': 1.5},
            {'type': 'integer', 'minimum': float('nan')}, {'type': 'integer', 'maximum': float('inf')},
            {'type': 'integer', 'enum': []}, {'type': 'integer', 'enum': [64, 64]},
            {'type': 'integer', 'enum': [False, 64]}, {'type': 'integer', 'enum': '64'},
            {'type': 'integer', 'minimum': 65, 'enum': [64]}, {'type': 'integer', 'minLength': 1},
            {'type': 'string'}, {'type': 'array'}, {'type': 'integer', 'default': 64},
        ]
        for rule in invalid_rules:
            bad = deepcopy(original)
            bad['async_media']['operations']['video.text_to_video']['request_schema']['properties']['width'] = rule
            with self.subTest(rule=rule), self.assertRaises(ValidationError):
                validate_async_media_specification(bad, required=True)
        for invalid_required in ([{}], [True], ['prompt', 'prompt'], ['prompt', 'unknown']):
            bad = deepcopy(original)
            bad['async_media']['operations']['video.text_to_video']['request_schema']['required'] = invalid_required
            with self.subTest(required=invalid_required), self.assertRaises(ValidationError):
                validate_async_media_specification(bad, required=True)
        for key in ('request_schema', 'smoke_inputs'):
            bad = deepcopy(original)
            bad['async_media']['operations']['video.text_to_video'][key] = None
            with self.assertRaises(ValidationError): validate_async_media_specification(bad, required=True)
        contract = original['async_media']['operations']['video.text_to_video']
        contract['request_schema']['properties'].update(audio={'type':'boolean'}, seed={'type':'integer','enum':[0,1]})
        contract['smoke_inputs'].update(audio=False, seed=0)
        validate_async_media_specification(original, required=True)
        self.assertEqual(validate_standard_payload(contract, contract['smoke_inputs']), contract['smoke_inputs'])
        for invalid in (False, float('inf'), '0'):
            with self.assertRaises(ValidationError): validate_standard_payload(contract, {**contract['smoke_inputs'], 'seed':invalid})
        self.assertNotIn('default', contract['request_schema']['properties']['seed'])

    def test_authoring_roundtrip_and_invalid_writes_preserve_facts(self):
        original = _async_spec()
        original['capabilities'] = ['video.text_to_video']
        original['model_family'] = 'async-authoring'
        del original['async_media']['operations']['image.text_to_image']
        invalid = deepcopy(original)
        invalid['async_media']['operations']['video.text_to_video']['request_schema']['properties']['width']['maximum'] = 1
        counts = ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()
        response = self.client.post(self.url, self.payload(invalid), format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual((ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()), counts)
        response = self.client.post(self.url, self.payload(original), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        detail = f"{self.url}{response.data['id']}/"
        changed = deepcopy(original)
        changed['async_media']['operations']['video.text_to_video']['smoke_inputs']['fps'] = 30
        response = self.client.patch(detail, {'specification': changed}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        count = PlatformLifecycleAuditEvent.objects.count()
        response = self.client.patch(detail, {'specification': invalid}, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.client.get(detail).data['specification'], changed)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), count)
        response = self.client.post(f'{detail}publish/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        sha = self.client.get(detail).data['specification_sha256']
        self.assertEqual(self.client.patch(detail, {'specification': original}, format='json').status_code, 400)
        counts = ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()
        response = self.client.post(f'{detail}draft-next/', {'specification': invalid}, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual((ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()), counts)
        response = self.client.post(f'{detail}draft-next/', {'specification': original}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['specification'], original)
        self.assertEqual(self.client.get(detail).data['specification_sha256'], sha)
        draft = ModelProfileVersion.objects.get(pk=response.data['id'])
        assign_json_contract(draft, 'specification', invalid)
        draft.save()
        count = PlatformLifecycleAuditEvent.objects.count()
        self.assertEqual(self.client.post(f'{self.url}{draft.pk}/publish/', {}, format='json').status_code, 400)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'draft')
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), count)
