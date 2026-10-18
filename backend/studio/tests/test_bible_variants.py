from copy import deepcopy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from PIL import Image
from rest_framework.test import APIClient

from studio.models import Asset, BibleVariant, BibleVariantRevision, BibleVariantReview, Shot
from studio.services.bible_variants import create_variant
from studio.tests.factories import build_story


class BibleVariantTests(TestCase):
    def setUp(self):
        self.user, self.project, self.character, self.scene, *_ = build_story('variant-owner')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.enterContext(patch('studio.services.provider_registry._request_json', side_effect=AssertionError('No external calls')))
        self.enterContext(patch('studio.services.provider_registry.resolve_credential', side_effect=AssertionError('No credentials')))
        self.url = '/api/v1/bible-variants/'
        self.value = {'name': '制服', 'reference_name': '凛・制服', 'identity_anchor': '共用面部身份',
            'appearance_anchor': '本造型的完整服装状态', 'basis_asset_id': None, 'basis_asset_sha256': ''}
        self.primary_input = {'appearance_role': 'primary', 'primary_setting_revision_id': None, 'primary_setting_sha256': ''}

    def create(self, **changes):
        primary = changes.pop('primary', None)
        relation = self.primary_input if primary is None else {'appearance_role': 'variant',
            'primary_setting_revision_id': primary['setting']['id'],
            'primary_setting_sha256': primary['setting']['content_sha256'], 'identity_anchor': ''}
        response = self.client.post(self.url, {'project_id': str(self.project.pk), 'entity_kind': 'character',
            'entity_id': str(self.character.pk), **self.value, **relation, **changes}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def detail_url(self, row):
        return f"{self.url}{row['id']}/"

    def review(self, row, **changes):
        return self.client.post(self.detail_url(row) + 'review/', {'expected_revision': row['revision'],
            'expected_sha256': row['setting']['content_sha256'], 'decision': 'approve', 'comment': '', **changes}, format='json')

    def revise(self, row, **changes):
        return self.client.post(self.detail_url(row) + 'revise/', {
            **self.value, 'expected_revision': row['revision'], **changes}, format='json')

    def test_multilingual_names_and_multiple_variants_share_identity_not_versions(self):
        primary = self.create(reference_name='凛・主图')
        rows = [self.create(primary=primary, reference_name=name, name=name) for name in ('凛・制服', 'Lina · evening', 'لينا')]
        self.assertEqual(len({row['id'] for row in rows}), 3)
        for row in rows:
            self.assertEqual(row['revision'], 1)
            self.assertIsNone(row['current_decision'])
            self.assertIsNone(row['setting']['review'])
            self.assertEqual(row['setting']['identity_anchor'], '')
            self.assertEqual(row['primary_setting']['id'], primary['setting']['id'])
        self.character.refresh_from_db()
        self.assertEqual(self.character.name, '林默')
        self.assertNotIn("visual_identity", {field.name for field in self.character._meta.fields})

    def test_review_is_explicit_idempotent_and_not_media_adoption(self):
        row = self.create()
        self.assertIs(type(row['created_by_id']), int)
        self.assertEqual(row['created_by_id'], self.user.pk)
        self.assertIs(type(row['setting']['created_by_id']), int)
        first = self.review(row)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data['current_decision'], 'approve')
        self.assertIs(type(first.data['setting']['review']['decided_by_id']), int)
        self.assertEqual(first.data['setting']['review']['decided_by_id'], self.user.pk)
        replay = self.review(row)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(first.data, replay.data)
        self.assertEqual(BibleVariantReview.objects.count(), 1)
        self.assertEqual(self.review(row, decision='reject').status_code, 409)
        self.character.refresh_from_db()
        self.assertEqual(self.character.reference_assets.count(), 0)

    def test_new_revision_never_inherits_review_and_old_version_remains_readable(self):
        row = self.create()
        self.assertEqual(self.review(row).status_code, 200)
        response = self.revise(row, appearance_anchor='新造型', reference_name='任意语言的新版名称')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['revision'], 2)
        self.assertIsNone(response.data['current_decision'])
        self.assertIsNone(response.data['setting']['review'])
        old = self.client.get(self.detail_url(row), {'revision': 1})
        self.assertEqual(old.status_code, 200, old.data)
        self.assertEqual(old.data['revision'], 2)
        self.assertIsNone(old.data['current_decision'])
        self.assertEqual(old.data['setting']['review']['decision'], 'approve')
        self.assertEqual(old.data['setting']['reference_name'], self.value['reference_name'])
        self.assertEqual(self.revise(row, appearance_anchor='过期编辑').status_code, 409)
        self.assertEqual(self.review(row).status_code, 409)

    def test_digest_noop_and_unknown_fields_fail_without_writes(self):
        row = self.create()
        self.assertEqual(self.review(row, expected_sha256='0' * 64).status_code, 409)
        self.assertEqual(self.revise(row).status_code, 400)
        self.assertEqual(self.revise(row, unknown=True).status_code, 400)
        self.assertEqual(BibleVariantRevision.objects.count(), 1)
        self.assertEqual(BibleVariantReview.objects.count(), 0)

    def test_ownership_and_exact_entity_project(self):
        other, project, character, *_ = build_story('variant-other')
        response = self.client.post(self.url, {'project_id': str(self.project.pk), 'entity_kind': 'character',
            'entity_id': str(character.pk), **self.value, **self.primary_input}, format='json')
        self.assertEqual(response.status_code, 404, response.data)
        row = self.create()
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.detail_url(row)).status_code, 404)
        self.assertEqual(self.review(row).status_code, 404)
        self.assertEqual(self.client.get(self.url, {'project_id': str(self.project.pk),
            'entity_kind': 'character', 'entity_id': str(self.character.pk)}).status_code, 404)

    def test_unicode_equivalent_reference_names_conflict(self):
        existing = self.create(reference_name='ＡＢＣ')
        response = self.client.post(self.url, {'project_id': str(self.project.pk), 'entity_kind': 'scene',
            'entity_id': str(self.scene.pk), **self.value, **self.primary_input, 'reference_name': 'abc'}, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(BibleVariant.objects.count(), 1)
        for text in ('人物', self.character.name, str(self.character.pk), existing['id'], '主图', '制服', 'ＡＢＣ'):
            self.assertIn(text, str(response.data))

    def test_reference_name_conflict_on_revision_identifies_variant_without_writes(self):
        primary = self.create(reference_name='主图')
        first = self.create(primary=primary, name='雨衣', reference_name='夜間・雨衣')
        second = self.create(primary=primary, name='制服', reference_name='夜間・制服')
        before = list(BibleVariantRevision.objects.order_by('id').values())
        response = self.revise(second, identity_anchor='', reference_name=first['reference_name'])
        self.assertEqual(response.status_code, 409, response.data)
        for text in ('变体', '雨衣', first['id'], str(self.character.pk), first['reference_name']):
            self.assertIn(text, str(response.data))
        self.assertEqual(list(BibleVariantRevision.objects.order_by('id').values()), before)
        self.assertEqual(self.client.get(self.detail_url(second)).data['revision'], 1)

    def test_same_reference_name_in_other_project_does_not_leak_or_conflict(self):
        other, project, character, *_ = build_story('variant-reference-other')
        create_variant(actor=other, project_id=project.pk, entity_kind='character', entity_id=character.pk,
            **self.primary_input, value={**self.value, 'name': '另一项目私有名称'})
        own = self.create()
        self.assertEqual(own['reference_name'], self.value['reference_name'])
        response = self.client.post(self.url, {'project_id': str(self.project.pk), 'entity_kind': 'scene',
            'entity_id': str(self.scene.pk), **self.value, **self.primary_input}, format='json')
        self.assertEqual(response.status_code, 409, response.data)
        for text in ('另一项目私有名称', str(project.pk), str(character.pk)):
            self.assertNotIn(text, str(response.data))

    def test_multilingual_rename_keeps_ids_other_same_name_objects_and_shot_text(self):
        primary = self.create(name='林默', reference_name='林默・主图')
        sibling = self.create(entity_kind='scene', entity_id=str(self.scene.pk), name='林默', reference_name='林默・场景')
        shots = Shot.objects.filter(episode__project=self.project).order_by('id')
        self.assertTrue(shots.exists())
        shots.update(dialogue=[{'speaker_id': 'S1', 'text': '林默，你看见林默了吗？'}])
        before_shots = list(shots.values('id', 'narrative', 'action', 'dialogue'))
        response = self.revise(primary, name='リン・لينا', reference_name='لينا・日常')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['id'], primary['id'])
        self.assertEqual(response.data['character_id'], primary['character_id'])
        self.assertEqual(response.data['setting']['name'], 'リン・لينا')
        self.assertEqual(self.client.get(self.detail_url(sibling)).data['setting']['name'], '林默')
        self.character.refresh_from_db()
        self.assertEqual(self.character.name, '林默')
        self.assertEqual(list(shots.values('id', 'narrative', 'action', 'dialogue')), before_shots)

    def test_list_and_history_are_bounded_summaries(self):
        row = self.create()
        self.assertEqual(self.revise(row, appearance_anchor='新版').status_code, 200)
        listing = self.client.get(self.url, {'project_id': str(self.project.pk), 'entity_kind': 'character',
            'entity_id': str(self.character.pk), 'page_size': 1})
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertEqual(len(listing.data['results']), 1)
        self.assertNotIn('setting', listing.data['results'][0])
        history = self.client.get(self.detail_url(row) + 'revisions/', {'page_size': 1})
        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual(history.data['count'], 2)
        self.assertNotIn('appearance_anchor', history.data['results'][0])
        self.assertIsNotNone(history.data['next'])
        self.assertEqual(self.client.get(self.url).status_code, 400)
        self.assertEqual(self.client.get(self.detail_url(row), {'revision': '9' * 100}).status_code, 400)
        self.assertEqual(self.client.get(self.detail_url(row) + 'revisions/', {'page_size': 51}).status_code, 400)
        self.assertEqual(self.client.get(self.detail_url(row) + 'revisions/', {'unknown': 'ignored'}).status_code, 400)

    def test_text_capacity_and_input_types_apply_to_internal_writer(self):
        from rest_framework.exceptions import ValidationError as ApiValidationError
        for changes in ({'identity_anchor': None}, {'appearance_anchor': {}}, {'appearance_anchor': '字' * 6000},
                        {'reference_name': '<Picture 1>'}, {'reference_name': 'a\u200bb'}):
            with self.subTest(changes=list(changes)), self.assertRaises(ApiValidationError):
                create_variant(actor=self.user, project_id=self.project.pk, entity_kind='character',
                    entity_id=self.character.pk, **self.primary_input, value={**self.value, **changes})
        self.assertEqual(BibleVariant.objects.count(), 0)

    def test_blank_creative_settings_can_be_saved_filled_and_cleared_without_images(self):
        before = (self.character.name, self.character.description)
        primary = self.create(reference_name='主图依据', identity_anchor='', appearance_anchor='')
        rows = [self.create(primary=primary, name=name, reference_name=name, identity_anchor='', appearance_anchor='')
            for name in ('待补充甲', '待补充乙')]
        self.assertEqual(BibleVariant.objects.count(), 3)
        for row in rows:
            self.assertEqual(row['setting']['identity_anchor'], '')
            self.assertEqual(row['setting']['appearance_anchor'], '')
            self.assertIsNone(row['setting']['basis_asset_id'])
            self.assertIsNone(row['setting']['review'])
            self.assertNotIn('edit_requirements', row)
        filled = self.revise(rows[0], name='已补充', reference_name='已补充', identity_anchor='')
        self.assertEqual(filled.status_code, 200, filled.data)
        self.assertEqual(self.review(filled.data).status_code, 200)
        cleared = self.revise(filled.data, identity_anchor='', appearance_anchor='')
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertEqual(cleared.data['setting']['identity_anchor'], '')
        self.assertEqual(cleared.data['setting']['appearance_anchor'], '')
        self.assertIsNone(cleared.data['setting']['review'])
        prior = self.client.get(self.detail_url(rows[0]), {'revision': 2})
        self.assertEqual(prior.data['setting']['review']['decision'], 'approve')
        self.assertEqual(prior.data['setting']['identity_anchor'], '')
        self.assertEqual(prior.data['setting']['appearance_anchor'], self.value['appearance_anchor'])
        self.character.refresh_from_db()
        self.assertEqual((self.character.name, self.character.description), before)
        self.assertEqual(self.character.reference_assets.count(), 0)

    def test_empty_is_allowed_but_omitted_or_invalid_fields_are_not_defaulted(self):
        for field, bad in (('identity_anchor', None), ('appearance_anchor', []), ('name', ''), ('reference_name', '')):
            response = self.client.post(self.url, {'project_id': str(self.project.pk), 'entity_kind': 'character',
                'entity_id': str(self.character.pk), **self.value, **self.primary_input, field: bad}, format='json')
            self.assertEqual(response.status_code, 400, response.data)
        payload = {'project_id': str(self.project.pk), 'entity_kind': 'character',
            'entity_id': str(self.character.pk), **self.value, **self.primary_input}
        del payload['identity_anchor']
        self.assertEqual(self.client.post(self.url, payload, format='json').status_code, 400)
        self.assertEqual(BibleVariant.objects.count(), 0)

    def test_revision_and_review_are_append_only_even_with_reconstructed_pk(self):
        row = self.create()
        self.assertEqual(self.review(row).status_code, 200)
        for cls in (BibleVariantRevision, BibleVariantReview):
            obj = cls.objects.get()
            with self.assertRaises(ValidationError): obj.save()
            with self.assertRaises(ValidationError): obj.delete()
            with self.assertRaises(ValidationError): cls.objects.filter(pk=obj.pk).delete()
            with self.assertRaises(ValidationError): cls.objects.filter(pk=obj.pk).update(updated_at=obj.updated_at)
            copied = deepcopy(obj)
            copied._state.adding = True
            with self.assertRaises(IntegrityError), transaction.atomic(): copied.save()
            self.assertEqual(cls.objects.count(), 1)

    def test_manual_variants_pin_primary_revision_without_identity_copy_or_auto_rebind(self):
        primary = self.create(reference_name='主图')
        new_primary = self.revise(primary, reference_name='主图新版', identity_anchor='更新身份')
        self.assertEqual(new_primary.status_code, 200, new_primary.data)
        rows = [self.create(primary=primary, name=name, reference_name=name) for name in ('雨衣', '制服')]
        for row in rows:
            self.assertEqual(row['setting']['primary_setting_revision_id'], primary['setting']['id'])
            self.assertEqual(row['primary_setting']['identity_anchor'], self.value['identity_anchor'])
            self.assertEqual(row['primary_setting']['revision'], 1)
            changed = self.revise(row, reference_name=row['reference_name'], identity_anchor='', appearance_anchor='只改差异')
            self.assertEqual(changed.status_code, 200, changed.data)
            self.assertEqual(changed.data['primary_setting']['id'], primary['setting']['id'])
            self.assertIsNone(changed.data['setting']['basis_asset_id'])
            self.assertIsNone(changed.data['setting']['review'])
        count = BibleVariantRevision.objects.count()
        self.assertEqual(self.revise(rows[0], expected_revision=2, identity_anchor='重复共同身份').status_code, 400)
        self.assertEqual(BibleVariantRevision.objects.count(), count)

    def test_explicit_primary_relation_rejects_wrong_targets_and_never_writes_partial_rows(self):
        primary = self.create(reference_name='主图')
        sibling = self.create(primary=primary, reference_name='现有变体')
        other = self.create(entity_kind='scene', entity_id=str(self.scene.pk), reference_name='场景主图')
        request = {'project_id': str(self.project.pk), 'entity_kind': 'character', 'entity_id': str(self.character.pk),
            **self.value, 'identity_anchor': '', 'reference_name': '新变体', 'appearance_role': 'variant',
            'primary_setting_revision_id': primary['setting']['id'], 'primary_setting_sha256': primary['setting']['content_sha256']}
        for changes, status in (({'primary_setting_revision_id': None}, 400),
                ({'primary_setting_sha256': '0' * 64}, 409),
                ({'primary_setting_revision_id': sibling['setting']['id']}, 400),
                ({'primary_setting_revision_id': other['setting']['id']}, 400),
                ({'identity_anchor': '不能复制主图身份'}, 400),
                ({**self.primary_input}, 409)):
            with self.subTest(changes=changes):
                response = self.client.post(self.url, {**request, **changes}, format='json')
                self.assertEqual(response.status_code, status, response.data)
                self.assertEqual(BibleVariant.objects.count(), 3)
                self.assertEqual(BibleVariantRevision.objects.count(), 3)
        for field in ('appearance_role', 'primary_setting_revision_id', 'primary_setting_sha256'):
            incomplete = dict(request); del incomplete[field]
            self.assertEqual(self.client.post(self.url, incomplete, format='json').status_code, 400)
        self.assertEqual(self.revise(sibling, primary_setting_revision_id=other['setting']['id']).status_code, 400)

    def test_basis_uses_real_image_digest_and_rechecks_approval_but_allows_rejection(self):
        folder = self.enterContext(TemporaryDirectory())
        path = Path(folder) / 'basis.png'
        Image.new('RGB', (12, 12), 'white').save(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        asset = Asset.objects.create(owner=self.user, kind='character', name='基础图', mime_type='image/png',
            local_path=str(path), checksum=digest)
        row = self.create(identity_anchor='', basis_asset_id=str(asset.pk), basis_asset_sha256=digest)
        Image.new('RGB', (12, 12), 'black').save(path)
        self.assertEqual(self.review(row).status_code, 409)
        response = self.review(row, decision='reject')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['current_decision'], 'reject')

    def test_unavailable_and_technical_assets_cannot_be_identity_bases(self):
        folder = self.enterContext(TemporaryDirectory())
        path = Path(folder) / 'basis.png'
        Image.new('RGB', (12, 12), 'white').save(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for attributes in ({'is_active': False}, {'metadata': {'fixture_scope': 'workflow_smoke'}}, {'local_path': str(path) + '.missing'}):
            asset = Asset.objects.create(**{'owner': self.user, 'kind': 'character', 'name': '不可用',
                'mime_type': 'image/png', 'local_path': str(path), 'checksum': digest, **attributes})
            response = self.client.post(self.url, {'project_id': str(self.project.pk), 'entity_kind': 'character',
                'entity_id': str(self.character.pk), **self.value, **self.primary_input, 'basis_asset_id': str(asset.pk),
                'basis_asset_sha256': digest}, format='json')
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(BibleVariant.objects.count(), 0)
