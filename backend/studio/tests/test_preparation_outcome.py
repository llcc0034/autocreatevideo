"""Status explanation only: no model calls, database writes or inferred success."""
from types import SimpleNamespace
from django.test import SimpleTestCase
from studio.services.execution_preparation_presentation import preparation_outcome


class PreparationOutcomeTests(SimpleTestCase):
    def test_call_and_preparation_state_matrix(self):
        cases = [
            ("blocked", None, "", "输入准备受阻"),
            ("dispatched", None, "", "未读取到主调用记录"),
            ("preparing", None, "", "尚未创建"),
            ("dispatched", "queued", "", "已派发不代表"),
            ("dispatched", "failed", "", "主调用失败"),
            ("dispatched", "failed", "uncertain_remote_response", "远端结果尚不确定"),
            ("dispatched", "uncertain", "", "远端结果尚不确定"),
            ("blocked", "preparing", "", "输入准备受阻"),
            ("dispatched", "cancelled", "", "不再等待"),
            ("dispatched", "cancel_requested", "", "尚未确认远端终态"),
            ("dispatched", "awaiting_review", "", "输出仍待处理"),
            ("dispatched", "succeeded", "", "未读取到关联产物"),
            ("cancelled", None, "", "不会继续准备"),
            ("cancelled", "submitted", "", "停止准备不等于远端已经结束"),
            ("cancelled", "failed", "uncertain_remote_response", "尚未确认终态"),
        ]
        for status, task_status, error, message in cases:
            with self.subTest(status=status, task=task_status, error=error):
                task = SimpleNamespace(status=task_status, error_code=error, ignore_late_result=False) if task_status else None
                result = preparation_outcome(SimpleNamespace(status=status), task, auxiliary=False)
                self.assertIn(message, result["message"])
                self.assertEqual(result["artifact_count"], 0)

    def test_auxiliary_success_and_stopped_adoption_are_not_media_success(self):
        prep = SimpleNamespace(status="dispatched")
        task = SimpleNamespace(status="succeeded", error_code="", ignore_late_result=False)
        self.assertIn("不代表主调用成功", preparation_outcome(prep, task, auxiliary=True)["message"])
        task.ignore_late_result = True
        self.assertIn("已停止采用", preparation_outcome(prep, task, auxiliary=False)["message"])

    def test_independent_text_and_asset_are_not_production_selection(self):
        prep = SimpleNamespace(status="dispatched")
        task = SimpleNamespace(status="succeeded", error_code="", ignore_late_result=False)
        text = preparation_outcome(prep, task, auxiliary=False, asset_job=True, text_result=True)
        self.assertIn("已保存文本结果", text["message"])
        self.assertEqual(text["artifact_count"], 0)
        asset = preparation_outcome(prep, task, auxiliary=False, asset_job=True, artifacts=1)
        self.assertIn("入库不代表已被下游使用", asset["message"])
        self.assertEqual(asset["selected_count"], 0)
