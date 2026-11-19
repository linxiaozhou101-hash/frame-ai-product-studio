"""Exercise real Streamlit widgets; only cloud and automatic inference are mocked."""
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from PIL import Image, ImageDraw
from streamlit.testing.v1 import AppTest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from bailian import ApiError
from config import Settings
from protection import prepare_product as real_prepare, compose_result


def png(image):
    data = BytesIO()
    image.save(data, format="PNG")
    return data.getvalue()


def product_mask(size):
    mask = Image.new("L", size, 0)
    w, h = size
    ImageDraw.Draw(mask).rectangle((w//4, h//4, 3*w//4, 3*h//4), fill=255)
    return png(mask)


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        self.source = png(Image.new("RGB", (512, 512), "#456b55"))
        self.raw = png(Image.new("RGB", (512, 512), "#ece4d5"))
        self.settings = Settings("test-only-placeholder", poll_interval_seconds=0.001)
        config_patch = patch("config.Settings.from_env", return_value=self.settings)
        self.config_mock = config_patch.start()
        self.addCleanup(config_patch.stop)

        def infer(product, mask_data=None):
            with Image.open(BytesIO(product.preview_data)) as original:
                manual = mask_data if mask_data is not None else product_mask(original.size)
            return real_prepare(product, mask_data=manual)
        prepare_patch = patch("protection.prepare_product", side_effect=infer)
        self.prepare_mock = prepare_patch.start()
        self.addCleanup(prepare_patch.stop)
        self.client = Mock()
        self.client.settings = self.settings
        self.client.submit.return_value = "test-task-123"
        self.client.query.return_value = self.success()
        self.client.download.return_value = self.raw
        manager = MagicMock()
        manager.__enter__.return_value = self.client
        client_patch = patch("bailian.BailianClient", return_value=manager)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        network_patch = patch("requests.sessions.Session.request", side_effect=AssertionError("No real API in tests"))
        network_patch.start()
        self.addCleanup(network_patch.stop)

    @staticmethod
    def success():
        return {"task_status": "SUCCEEDED", "request_id": "test-success",
                "results": [{"url": "https://example.invalid/result.png"}]}

    def new_app(self):
        app = AppTest.from_file(str(PROJECT / "app.py"), default_timeout=20).run()
        self.assertFalse(app.exception)
        return app

    def upload(self, app, style="木质桌面", prepare=True):
        app.file_uploader[0].set_value(("product.png", self.source, "image/png")).run()
        app.radio[0].set_value(style).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key="generate").disabled)
        if prepare:
            app.button(key="prepare").click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.button(key="generate").disabled)

    def test_generate_download_and_change_style_reuses_contour(self):
        app = self.new_app()
        self.upload(app)
        prepared = app.session_state["prepared"]
        app.button(key="generate").click().run()
        self.assertFalse(app.exception)
        self.client.submit.assert_called_once()
        sent, prompt = self.client.submit.call_args.args
        self.assertIs(sent, prepared)
        self.assertEqual(self.client.submit.call_args.kwargs, {})
        for detail in ("原木桌面", "形状", "颜色", "图案", "文字", "拍摄角度"):
            self.assertIn(detail, prompt)
        self.assertEqual(app.session_state["job"].result, compose_result(prepared, self.raw))
        self.assertEqual(app.download_button[0].label, "下载结果 PNG")
        self.assertTrue(any("生成完成" in item.value for item in app.success))
        self.assertEqual(app.session_state["job"].model, self.settings.model)
        app.radio[0].set_value("浅色展台").run()
        self.assertIsNone(app.session_state["job"])
        self.assertIs(app.session_state["prepared"], prepared)
        self.assertEqual(len(app.download_button), 0)
        self.assertEqual(app.file_uploader[0].value.getvalue(), self.source)
        self.prepare_mock.assert_called_once()

    def test_query_retry_keeps_upload_style_contour_and_existing_task(self):
        self.client.query.side_effect = [ApiError("网络连接失败", code="NetworkError"), self.success()]
        app = self.new_app()
        self.upload(app, "浅色展台")
        prepared = app.session_state["prepared"]
        app.button(key="generate").click().run()
        self.assertIn("网络连接失败", app.error[0].value)
        self.assertEqual(app.session_state["job"].task_id, "test-task-123")
        app.button(key="retry").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.radio[0].value, "浅色展台")
        self.assertEqual(app.file_uploader[0].value.getvalue(), self.source)
        self.assertIs(app.session_state["prepared"], prepared)
        self.client.submit.assert_called_once()
        self.prepare_mock.assert_called_once()
        self.assertEqual(len(app.download_button), 1)

    def test_download_retry_does_not_submit_or_query_again(self):
        self.client.download.side_effect = [ApiError("图片获取失败", code="DownloadError"), self.raw]
        app = self.new_app()
        self.upload(app)
        app.button(key="generate").click().run()
        app.button(key="retry").click().run()
        self.assertFalse(app.exception)
        self.client.submit.assert_called_once()
        self.client.query.assert_called_once()
        self.assertEqual(self.client.download.call_count, 2)
        self.assertEqual(len(app.download_button), 1)

    def test_new_product_does_not_reuse_old_manual_mask(self):
        app = self.new_app()
        self.upload(app, prepare=False)
        app.file_uploader[1].set_value(("mask.png", product_mask((512, 512)), "image/png")).run()
        app.button(key="prepare").click().run()
        self.assertIsNotNone(app.session_state["prepared"])
        other = png(Image.new("RGB", (512, 512), "#3855ab"))
        app.file_uploader[0].set_value(("other.png", other, "image/png")).run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.file_uploader[1].value)
        self.assertIsNone(app.session_state["prepared"])
        self.assertTrue(app.button(key="generate").disabled)
        self.client.submit.assert_not_called()

    def test_missing_key_keeps_preview_available(self):
        self.config_mock.side_effect = ValueError("请配置 DASHSCOPE_API_KEY。")
        app = self.new_app()
        self.upload(app, prepare=False)
        self.assertTrue(any("DASHSCOPE_API_KEY" in item.value for item in app.info))
        self.assertEqual(len(app.get("image")), 1)
        self.assertTrue(app.button(key="generate").disabled)
        self.client.submit.assert_not_called()

    def test_invalid_image_has_clear_error_and_no_submit(self):
        app = self.new_app()
        app.file_uploader[0].set_value(("broken.png", b"bad", "image/png")).run()
        self.assertFalse(app.exception)
        self.assertIn("无法读取", app.error[0].value)
        self.assertTrue(app.button(key="generate").disabled)
        self.client.submit.assert_not_called()

    def test_segmentation_failure_preserves_inputs_and_allows_retry(self):
        self.prepare_mock.side_effect = ValueError("本地识别失败，请重试")
        app = self.new_app()
        self.upload(app, prepare=False)
        app.button(key="prepare").click().run()
        self.assertFalse(app.exception)
        self.assertIn("本地识别失败", app.error[0].value)
        self.assertEqual(app.file_uploader[0].value.getvalue(), self.source)
        self.assertEqual(app.radio[0].value, "木质桌面")
        self.assertTrue(app.button(key="generate").disabled)
        self.assertFalse(app.button(key="prepare").disabled)
        self.client.submit.assert_not_called()

    def test_small_input_is_preserved_and_mask_follows_resize(self):
        self.source = png(Image.new("RGB", (240, 320), "#456b55"))
        app = self.new_app()
        self.upload(app)
        prepared = app.session_state["prepared"]
        self.assertEqual((prepared.width, prepared.height), (512, 683))
        app.button(key="generate").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.file_uploader[0].value.getvalue(), self.source)
        self.assertTrue(any("原图 240 × 320" in item.value for item in app.caption))
        self.assertEqual(len(app.download_button), 1)


if __name__ == "__main__":
    unittest.main()
