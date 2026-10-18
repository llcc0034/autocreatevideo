from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import ModelProfileVersion, PlatformLifecycleAuditEvent, ProviderProfile
from studio.platform_serializers import ModelProfileRegistryOptionsSerializer
from studio.services.json_contracts import assign_json_contract
from studio.services.model_profile_contracts import (
    chat_request_options_editor_options, validate_chat_request_options,
    verified_model_profile_specification,
)
from studio.services.model_profile_options import model_profile_registry_options


class ChatOptionsAuthoringTests(TestCase):
    def setUp(self):
        self.enterContext(patch('studio.services.provider_registry._request_json', side_effect=AssertionError('No network')))
        self.enterContext(patch('studio.services.provider_registry.resolve_credential', side_effect=AssertionError('No credentials')))
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_superuser(username='chat-authoring-test', password='IsolatedTestOnly123!'))
        self.provider = ProviderProfile(key='chat-authoring-test', name='Isolated', driver_type='openai_compatible', modalities='text')
        assign_json_contract(self.provider, 'driver_config', {'base_url': 'http://localhost', 'chat_path': '/v1/chat/completions', 'timeout_seconds': 30, 'capability_ttl_seconds': 3600})
        self.provider.save()
        self.url = '/api/v1/model-profile-versions/'

    def specification(self, options):
        return {'schema_version': 1, 'model_family': 'chat-authoring-test', 'capabilities': ['prompt.draft'],
                'text_execution': {'schema_version': 1, 'operations': {'prompt.draft': {'parameters': {}, 'chat_request_options': options}}}}

    def test_catalog_matches_validator_without_guessing_provider_limits(self):
        catalog = chat_request_options_editor_options()
        self.assertEqual(ModelProfileRegistryOptionsSerializer(model_profile_registry_options()).data['chat_request_options'], catalog)
        self.assertNotIn('temperature_maximum', catalog)
        for options in ({}, {'temperature': catalog['temperature_minimum']}, {'temperature': None, 'response_format': None},
                        {'temperature': 9, 'response_format': {'type': 'provider_specific', 'extra': False}},
                        *({'response_format': {'type': row['value']}} for row in catalog['response_format_types'])):
            snapshot = deepcopy(options)
            self.assertEqual(validate_chat_request_options(options), snapshot)
            self.assertEqual(options, snapshot)
        for options in ({'temperature': -1}, {'temperature': True}, {'temperature': float('inf')},
                        {'response_format': {}}, {'nested': {'max_tokens': 1}}, {'nested': {'api_key': 'not-a-credential'}}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                validate_chat_request_options(options)

    def test_create_patch_publish_and_successor_preserve_options_and_audit(self):
        original = self.specification({'temperature': 0, 'response_format': {'type': 'json_object'}, 'thinking': {'type': 'disabled'}})
        response = self.client.post(self.url, {'profile_key': 'chat-authoring-test', 'provider': str(self.provider.pk), 'modality': 'text', 'model_identifier': 'isolated', 'specification': original}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        detail = f"{self.url}{response.data['id']}/"
        changed = deepcopy(original)
        changed['text_execution']['operations']['prompt.draft']['chat_request_options']['temperature'] = None
        response = self.client.patch(detail, {'specification': changed}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        response = self.client.post(f'{detail}publish/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        snapshot = self.client.get(detail).data
        self.assertEqual(snapshot['specification'], changed)
        counts = ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()
        invalid = self.specification({'temperature': -1})
        for suffix, payload in (('draft-next/', {'specification': invalid}), ('', {'specification': original})):
            response = self.client.post(f'{detail}{suffix}', payload, format='json') if suffix else self.client.patch(detail, payload, format='json')
            self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual((ModelProfileVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()), counts)
        response = self.client.post(f'{detail}draft-next/', {'specification': original}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        model = ModelProfileVersion.objects.get(pk=response.data['id'])
        self.assertEqual(verified_model_profile_specification(model), original)
        self.assertEqual(self.client.get(detail).data['specification_sha256'], snapshot['specification_sha256'])
        # Publish rechecks malformed stored drafts; it must not repair them silently.
        assign_json_contract(model, 'specification', invalid)
        model.save()
        response = self.client.post(f'{self.url}{model.pk}/publish/', {}, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        model.refresh_from_db()
        self.assertEqual(model.status, 'draft')
        self.assertEqual(verified_model_profile_specification(model), invalid)

    def test_api_rejects_inline_output_schema_without_writing_a_model(self):
        before = ModelProfileVersion.objects.count()
        for format in ({"type": "json_schema", "json_schema": {"schema": {"type": "object"}}},
                {"type": "json_schema", "schema": {"type": "object"}},
                {"type": "json_schema", "json_schema": None},
                {"type": "json_schema", "json_schema": {"strict": "false"}},
                {"type": "json_object", "json_schema": {"name": "obsolete"}}):
            with self.subTest(format=format):
                response = self.client.post(self.url, {"profile_key": "no-inline", "provider": str(self.provider.pk),
                    "modality": "text", "model_identifier": "isolated", "specification": self.specification({"response_format": format})}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(ModelProfileVersion.objects.count(), before)
