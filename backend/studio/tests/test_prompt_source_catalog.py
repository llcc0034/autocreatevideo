from django.test import SimpleTestCase

from studio.services.prompt_profiles import SUPPORTED_FIELD_SOURCE_SELECTORS
from studio.services.prompt_source_catalog import prompt_source_options
from studio.platform_serializers import PromptSourceOptionSerializer


class PromptSourceCatalogTests(SimpleTestCase):
    def test_complete_unique_readonly_catalog_matches_supported_vocabulary(self):
        rows = prompt_source_options(SUPPORTED_FIELD_SOURCE_SELECTORS)
        self.assertEqual([row['value'] for row in rows], list(SUPPORTED_FIELD_SOURCE_SELECTORS))
        self.assertEqual(len(rows), len({row['value'] for row in rows}))
        for row in rows:
            serializer = PromptSourceOptionSerializer(data=row)
            self.assertTrue(serializer.is_valid(), serializer.errors)
            self.assertEqual(set(row), set(serializer.validated_data))
            self.assertTrue(all(isinstance(value, str) and value.strip() for value in row.values()))

    def test_request_copy_is_not_an_implicit_project_or_latest_output_lookup(self):
        rows = {row['value']: row for row in prompt_source_options(SUPPORTED_FIELD_SOURCE_SELECTORS)}
        self.assertIn('确认填入', rows['request.theme']['description'])
        self.assertIn('不附带', rows['request.style']['description'])
        self.assertIn('选择有权读取的项目、预览并确认', rows['project.logline']['execution_usage'])
        self.assertIn('不接受手工冒充项目事实', rows['project.logline']['execution_usage'])
        self.assertIn('request.style', rows['style.visual_prompt']['execution_usage'])
        self.assertIn('不自动采用其他任务的最新输出', rows['request.theme']['execution_usage'])

    def test_defaults_and_derived_results_are_not_user_input_aliases(self):
        rows = {row['value']: row for row in prompt_source_options(SUPPORTED_FIELD_SOURCE_SELECTORS)}
        self.assertIn('不生成本次输入接线', rows['fixed.value']['execution_usage'])
        self.assertIn('发布策略时冻结', rows['fixed.value']['description'])
        self.assertIn('不能把来源标识当作待填正文', rows['projection.output']['execution_usage'])
        self.assertNotIn('request.h3_ref2va.summary', rows)

    def test_request_values_explain_automatic_inputs_without_claiming_asset_autowiring(self):
        rows = {row['value']: row for row in prompt_source_options(SUPPORTED_FIELD_SOURCE_SELECTORS)}
        self.assertIn('不必在实例重复接线', rows['request.theme']['execution_usage'])
        self.assertIn('明确映射', rows['request.style']['execution_usage'])
        self.assertIn('明确配置素材槽位', rows['request.reference_assets']['execution_usage'])
        self.assertNotIn('自动生成此输入', rows['request.reference_assets']['execution_usage'])

    def test_unsupported_source_does_not_receive_invented_help(self):
        with self.assertRaises(KeyError):
            prompt_source_options(['request.unregistered'])
