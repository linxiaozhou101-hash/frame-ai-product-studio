"""Exercise retries without making network calls or creating paid tasks."""

import unittest
from unittest.mock import Mock, call, patch
from io import BytesIO
from PIL import Image, ImageDraw
from config import Settings
from protection import prepare_product, compose_result
from images import read_product_image

from bailian import ApiError
from images import ProductImage
from prompts import STYLES, build_prompt
from workflow import Job, run_job


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        def png(image):
            out = BytesIO()
            image.save(out, format="PNG")
            return out.getvalue()
        self.source = png(Image.new("RGB", (512, 512), "#456b55"))
        self.background = png(Image.new("RGB", (512, 512), "#ece4d5"))
        mask = Image.new("L", (512, 512), 0)
        ImageDraw.Draw(mask).rectangle((150, 120, 360, 400), fill=255)
        self.image = read_product_image(self.source)
        self.prepared = prepare_product(self.image, png(mask))
        self.expected_result = compose_result(self.prepared, self.background)
        self.job = Job(self.image, "木质桌面", prepared=self.prepared)
        self.client = Mock()
        self.client.settings = Settings("test-only-placeholder")
        self.client.submit.return_value = "task-original"
        self.client.download.return_value = self.background
        self.clock = FakeClock()
        self.updates = []

    @staticmethod
    def success():
        return {
            "task_status": "SUCCEEDED",
            "request_id": "request-success",
            "results": [{"url": "https://example.com/generated.png"}],
        }

    def run_attempt(self, timeout=20):
        run_job(
            self.job,
            self.client,
            poll_interval=2,
            timeout=timeout,
            on_update=lambda phase, elapsed: self.updates.append((phase, elapsed)),
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def assert_inputs_retained(self):
        self.assertIs(self.job.image, self.image)
        self.assertEqual(self.job.image.data, self.source)
        self.assertEqual(self.job.style, "木质桌面")
        self.assertIs(self.job.prepared, self.prepared)
        for submitted in self.client.submit.call_args_list:
            self.assertIs(submitted.args[0], self.prepared)
            self.assertEqual(submitted.kwargs, {})

    def test_missing_protection_cannot_submit_whole_image(self):
        self.job.prepared = None
        self.run_attempt()
        self.assertEqual(self.job.error_code, "MissingProtection")
        self.client.submit.assert_not_called()

    def test_composition_failure_reuses_downloaded_bytes(self):
        self.client.query.return_value = self.success()
        with patch("workflow.compose_result", side_effect=ValueError("local failure")):
            self.run_attempt()
        self.assertEqual(self.job.error_code, "CompositionError")
        self.assertEqual(self.job.raw_result, self.background)
        self.job.retry()
        self.run_attempt()
        self.client.submit.assert_called_once()
        self.client.download.assert_called_once()
        self.assertEqual(self.job.result, self.expected_result)

    def test_endpoint_change_blocks_wrong_endpoint_resume(self):
        self.job.task_id = "original"
        self.job.base_url = "https://workspace.cn-beijing.maas.aliyuncs.com"
        self.run_attempt()
        self.assertEqual(self.job.error_code, "ConfigurationChanged")
        self.client.query.assert_not_called()
        self.client.submit.assert_not_called()

    def test_all_styles_preserve_product_details_and_replace_original_surface(self):
        for style in STYLES:
            with self.subTest(style=style):
                prompt = build_prompt(style)
                self.assertIn("背景", prompt)
                self.assertLessEqual(len(prompt), 800)
                for detail in ("形状", "颜色", "材质", "图案", "文字", "拍摄角度", "开口", "把手"):
                    self.assertIn(detail, prompt)
                self.assertIn("不给商品添加盖子或其他部件", prompt)

    def test_pedestal_prompt_specifies_white_plaster_below_product_without_wood(self):
        prompt = build_prompt("浅色展台")
        for detail in ("象牙白色", "石膏几何展台", "商品下方承托商品", "没有木纹"):
            self.assertIn(detail, prompt)

    def test_pending_running_success_downloads_once_and_reports_elapsed(self):
        self.client.query.side_effect = [
            {"task_status": "PENDING"},
            {"task_status": "RUNNING"},
            self.success(),
        ]

        self.run_attempt()

        self.client.submit.assert_called_once()
        image, prompt = self.client.submit.call_args.args
        self.assertIs(image, self.prepared)
        self.assertEqual(self.job.submitted_prompt, prompt)
        for text in ("原木桌面", "形状", "颜色", "图案", "文字", "拍摄角度"):
            self.assertIn(text, prompt)
        self.assertEqual(self.client.query.call_args_list, [call("task-original")] * 3)
        self.client.download.assert_called_once_with("https://example.com/generated.png")
        self.assertEqual(self.job.result, self.expected_result)
        self.assertEqual(self.job.task_status, "SUCCEEDED")
        self.assertEqual(self.job.request_id, "request-success")
        self.assertEqual(self.job.elapsed, 4)
        self.assertFalse(self.job.active)
        self.assertFalse(self.job.error)
        phases = [phase for phase, _ in self.updates]
        self.assertIn("任务排队中", phases)
        self.assertIn("正在生成新背景", phases)
        self.assertEqual(phases[-1], "生成成功")
        self.assert_inputs_retained()

    def test_submission_failure_preserves_inputs_and_can_retry(self):
        self.client.submit.side_effect = [
            ApiError("API Key 无效", code="InvalidApiKey", request_id="request-1"),
            "task-retry",
        ]
        self.run_attempt()

        self.assertEqual(self.job.error_code, "InvalidApiKey")
        self.assertEqual(self.job.request_id, "request-1")
        self.assertEqual(self.job.error, "API Key 无效")
        self.assertEqual(self.job.task_id, "")
        self.assertFalse(self.job.active)
        self.client.query.assert_not_called()
        self.client.download.assert_not_called()
        self.assert_inputs_retained()

        self.job.retry()
        self.assertTrue(self.job.active)
        self.assertEqual((self.job.error, self.job.error_code, self.job.request_id), ("", "", ""))
        self.client.query.return_value = self.success()
        self.run_attempt()

        self.assertEqual(self.client.submit.call_count, 2)
        self.client.query.assert_called_once_with("task-retry")
        self.assertEqual(self.job.result, self.expected_result)
        self.assert_inputs_retained()

    def test_terminal_task_states_stop_and_retry_creates_a_new_task(self):
        for status in ("FAILED", "CANCELED", "UNKNOWN"):
            with self.subTest(status=status):
                self.setUp()
                output = {"task_status": status, "code": "TaskError", "message": "任务未完成"}
                self.client.query.return_value = output
                self.client.task_error.return_value = ApiError(
                    "任务未完成", code="TaskError", request_id="request-2", terminal=True
                )
                self.run_attempt()

                self.client.task_error.assert_called_once_with(output)
                self.client.query.assert_called_once()
                self.client.download.assert_not_called()
                self.assertTrue(self.job.terminal_failure)
                self.assertFalse(self.job.active)
                self.assertIsNone(self.job.result)
                self.assertEqual(self.job.task_id, "task-original")
                self.assertEqual(self.job.task_status, status)
                self.assert_inputs_retained()

                self.job.retry()
                self.assertEqual(self.job.task_id, "")
                self.assertEqual(self.job.url, "")
                self.assertEqual(self.job.task_status, "")
                self.assertFalse(self.job.terminal_failure)
                self.client.submit.return_value = "task-retry"
                self.client.query.return_value = self.success()
                self.run_attempt()

                self.assertEqual(self.client.submit.call_count, 2)
                self.assertEqual(self.job.task_id, "task-retry")
                self.assertEqual(self.job.result, self.expected_result)
                self.assert_inputs_retained()

    def test_polling_timeout_retry_queries_existing_task_without_resubmitting(self):
        self.client.query.return_value = {"task_status": "RUNNING"}
        self.run_attempt(timeout=3)

        self.assertEqual(self.job.error_code, "PollingTimeout")
        self.assertEqual(self.job.elapsed, 3)
        self.assertEqual(self.client.query.call_count, 2)
        self.assertFalse(self.job.terminal_failure)
        self.assertEqual(self.job.task_id, "task-original")
        self.client.download.assert_not_called()

        # Time between the user's attempts is not counted as processing time.
        self.clock.sleep(60)
        self.job.retry()
        self.client.query.side_effect = [{"task_status": "RUNNING"}, self.success()]
        self.run_attempt(timeout=3)

        self.client.submit.assert_called_once()
        self.assertEqual(self.job.elapsed, 5)
        self.assertFalse(self.job.error)
        self.assertEqual(self.job.result, self.expected_result)
        self.assertEqual(self.updates[-1][1], 5)
        self.assert_inputs_retained()

    def test_query_network_failure_retry_resumes_same_task(self):
        self.client.query.side_effect = [
            ApiError("查询连接中断", code="NetworkError"),
            self.success(),
        ]
        self.run_attempt()
        self.assertEqual(self.job.error_code, "NetworkError")
        self.assertEqual(self.job.task_id, "task-original")
        self.client.download.assert_not_called()

        self.job.retry()
        self.run_attempt()

        self.client.submit.assert_called_once()
        self.assertEqual(self.client.query.call_args_list, [call("task-original")] * 2)
        self.client.download.assert_called_once()
        self.assertFalse(self.job.error)
        self.assert_inputs_retained()

    def test_download_retry_keeps_result_url_and_does_not_query_or_submit(self):
        self.client.query.return_value = self.success()
        self.client.download.side_effect = [
            ApiError("图片下载中断", code="DownloadError"),
            self.background,
        ]
        self.run_attempt()
        self.assertEqual(self.job.error_code, "DownloadError")
        self.assertEqual(self.job.url, "https://example.com/generated.png")
        self.assertEqual(self.job.task_id, "task-original")
        self.assertIsNone(self.job.result)

        self.job.retry()
        self.run_attempt()

        self.client.submit.assert_called_once()
        self.client.query.assert_called_once()
        self.assertEqual(
            self.client.download.call_args_list,
            [call("https://example.com/generated.png")] * 2,
        )
        self.assertEqual(self.job.result, self.expected_result)
        self.assertFalse(self.job.error)
        self.assert_inputs_retained()

    def test_unrecognized_status_stops_and_can_resume_without_duplicate_task(self):
        self.client.query.side_effect = [{"task_status": "NEW_STATUS"}, self.success()]
        self.run_attempt()
        self.assertEqual(self.job.error_code, "UnexpectedStatus")
        self.assertFalse(self.job.terminal_failure)
        self.client.download.assert_not_called()

        self.job.retry()
        self.run_attempt()

        self.client.submit.assert_called_once()
        self.assertEqual(self.job.result, self.expected_result)
        self.assert_inputs_retained()


if __name__ == "__main__":
    unittest.main()
