"""M6-08: obsolete fixed Shot columns never re-enter current Profile inputs.

Only isolated SQLite and explicit authoring fixtures; no model/Smoke dispatch.
"""
from copy import deepcopy
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import GenerationAttempt, PromptProfileFieldRule
from studio.services.execution_profile_inputs import profile_input_targets
from studio.services.prompt_profile_compiler import compile_prompt_profile_facts
from studio.services.prompt_profiles import (
    PromptProfileError, publish_prompt_profile, refresh_prompt_profile_contract,
    validate_prompt_profile_for_publish,
)
from studio.tests import test_prompt_profiles as profile_fixtures
from studio.tests.reference_rendering_fixtures import generation_structure_fixture, profile_compilation_fixture


RETIRED = tuple('shot.' + key for key in (
    'narrative', 'detailed_description', 'camera', 'action', 'sound_effects', 'music', 'negative_prompt',
))


class RetiredPromptSourceTests(TestCase):
    _field = profile_fixtures.PromptProfileServiceTests._field
    _profile = profile_fixtures.PromptProfileServiceTests._profile
    _rule = profile_fixtures.PromptProfileServiceTests._rule
    _model_profile = profile_fixtures.PromptProfileServiceTests._model_profile

    def setUp(self):
        self.assertIn('memory', str(settings.DATABASES['default']['NAME']))
        profile_fixtures.PromptProfileServiceTests.setUp(self)
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'studio.services.provider_registry.resolve_credential'):
            blocked = self.enterContext(patch(target, side_effect=AssertionError('No external calls')))
            self.addCleanup(blocked.assert_not_called)
        self._model_profile()
        self.profile = self._profile('qa.retired-sources', family='deepseek', operation='script.generate', mode='')
        self.field = self._field()
        self.rule = self._rule(self.profile, self.field, selector='request.text')
        self.client = APIClient()
        self.client.force_authenticate(self.actor)

    def test_options_schema_and_create_patch_reject_all_seven_without_mutation(self):
        options = self.client.get('/api/v1/prompt-profile-versions/options/')
        self.assertEqual(options.status_code, 200, options.data)
        choices = {row['value'] for row in options.data['source_selectors']}
        body = {'profile_version': str(self.profile.pk), 'field_definition': str(self.field.pk),
                'field_definition_version': str(self.rule.field_definition_version_id),
                'channel': 'positive_prompt', 'position': 2, 'generation_mode': 'passthrough'}
        before = self.profile.contract_sha256
        for source in RETIRED:
            with self.subTest(source=source):
                self.assertNotIn(source, choices)
                self.assertNotIn(source, PromptProfileFieldRule.SourceSelector.values)
                created = self.client.post('/api/v1/prompt-profile-field-rules/',
                    {**body, 'source_selector': source, 'generation_source_selector': source}, format='json')
                self.assertEqual(created.status_code, 400, created.data)
                changed = self.client.patch(f'/api/v1/prompt-profile-field-rules/{self.rule.pk}/',
                    {'source_selector': source, 'generation_source_selector': source}, format='json')
                self.assertEqual(changed.status_code, 400, changed.data)
                self.rule.refresh_from_db()
                self.profile.refresh_from_db()
                self.assertEqual(self.rule.source_selector, 'request.text')
                self.assertEqual(self.profile.contract_sha256, before)
                self.assertEqual(self.profile.field_rules.count(), 1)

    def test_saved_obsolete_rule_cannot_publish_project_inputs_or_compile(self):
        for source in RETIRED:
            with self.subTest(source=source):
                # Pre-cutover configuration, deliberately bypassing today's authoring validation.
                PromptProfileFieldRule.objects.filter(pk=self.rule.pk).update(
                    source_selector=source, generation_source_selector=source)
                refresh_prompt_profile_contract(self.profile)
                before = deepcopy(self.profile.field_rules.values().get())
                for operation in (
                    lambda: validate_prompt_profile_for_publish(self.profile),
                    lambda: profile_input_targets(self.profile),
                ):
                    with self.assertRaises(PromptProfileError) as error:
                        operation()
                    self.assertEqual(error.exception.code, 'prompt_profile_source_selector_unsupported')
                # Recreate the pre-cutover published state only in the isolated fixture.
                self.profile.status = 'published'
                with self.assertRaises(PromptProfileError) as error:
                    compile_prompt_profile_facts(self.profile, facts=profile_compilation_fixture(sources={}))
                self.assertEqual(error.exception.code, 'prompt_profile_source_selector_unsupported')
                self.profile.status = 'draft'
                self.assertEqual(self.profile.field_rules.values().get(), before)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_custom_structured_camera_action_and_setting_remain_exact_inputs(self):
        generation_structure_fixture(self.profile, properties=('camera', 'action', 'setting'))
        self.rule.source_selector = self.rule.generation_source_selector = 'request.structured_input'
        self.rule.source_path = ['action']
        self.rule.generation_mode = 'passthrough'
        self.rule.language_policy = 'any'
        self.rule.save()
        refresh_prompt_profile_contract(self.profile)
        profile = publish_prompt_profile(self.profile, actor=self.actor)
        sources = {'request.structured_input': {'action': ' 原文：回头望向门口。\n不删句 ',
                    'camera': '固定机位', 'setting': '雨夜；留作独立字段'}}
        before = deepcopy(sources)
        targets = profile_input_targets(profile)
        self.assertEqual(set(targets), {'request.structured_input'})
        result = compile_prompt_profile_facts(profile, facts=profile_compilation_fixture(sources=sources))
        # Existing channel formatting trims outer whitespace; it must not rewrite
        # the independent raw source or drop authored sentences/internal newlines.
        self.assertIn(sources['request.structured_input']['action'].strip(), result.assembly['channels']['positive_prompt'])
        self.assertNotIn('雨夜；留作独立字段', result.assembly['channels']['positive_prompt'])
        self.assertEqual(sources, before)
        self.assertFalse(GenerationAttempt.objects.exists())
