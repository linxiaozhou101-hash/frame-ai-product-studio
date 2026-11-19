"""HTTP contract checks using local responses; these do not call the paid API."""

import base64
from io import BytesIO
import os
import unittest
from unittest.mock import Mock, patch

import requests
from PIL import Image

from bailian import ApiError, BailianClient, result_url
from config import Settings
from protection import PreparedProduct
from dataclasses import replace


def response(data=None, status=200, content=b""):
    value = Mock(status_code=status, content=content)
    value.json.return_value = data
    return value


class BailianContractTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(api_key="test-secret-credential")
        self.client = BailianClient(self.settings)
        self.addCleanup(self.client.session.close)

    @staticmethod
    def prepared():
        def png(image):
            buf = BytesIO()
            image.save(buf, "PNG")
            return buf.getvalue()
        source = png(Image.new("RGB", (512, 512), "red"))
        alpha = Image.new("L", (512, 512), 0)
        alpha.paste(255, (128, 128, 384, 384))
        mask = Image.new("RGB", (512, 512), "white")
        mask.paste((0, 0, 0), (128, 128, 384, 384))
        return PreparedProduct(source, png(alpha), png(mask), 512, 512, "test", 0.25)

    def test_submit_uses_documented_mask_protocol_and_base64(self):
        prepared = self.prepared()
        reply = response({"output": {"task_id": "task-123", "task_status": "PENDING"}})
        with patch.object(self.client.session, "request", return_value=reply) as request:
            self.assertEqual(self.client.submit(prepared, "只更换背景"), "task-123")
        request.assert_called_once()
        args, kwargs = request.call_args
        self.assertEqual(args, ("POST", "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2image/image-synthesis"))
        self.assertEqual(kwargs["headers"], {
            "Authorization": "Bearer test-secret-credential",
            "Content-Type": "application/json", "X-DashScope-Async": "enable",
        })
        self.assertFalse(kwargs["allow_redirects"])
        body = kwargs["json"]
        self.assertEqual(body["model"], "wanx2.1-imageedit")
        self.assertEqual(body["input"]["function"], "description_edit_with_mask")
        self.assertEqual(body["input"]["prompt"], "只更换背景")
        for field, expected in [("base_image_url", prepared.source_png), ("mask_image_url", prepared.mask_png)]:
            prefix, encoded = body["input"][field].split(",", 1)
            self.assertEqual(prefix, "data:image/png;base64")
            self.assertEqual(base64.b64decode(encoded), expected)
        self.assertEqual(body["parameters"], {"n": 1, "watermark": False})
        self.assertNotIn("strength", body["parameters"])

    def test_missing_invalid_or_all_background_masks_never_submit(self):
        cases = [None, replace(self.prepared(), mask_png=b"broken")]
        for size, color in [((513, 512), "white"), ((512, 512), "gray"), ((512, 512), "white")]:
            buf = BytesIO()
            Image.new("RGB", size, color).save(buf, "PNG")
            cases.append(replace(self.prepared(), mask_png=buf.getvalue()))
        for prepared in cases:
            with self.subTest(prepared=type(prepared).__name__), patch.object(self.client.session, "request") as request:
                with self.assertRaises(ApiError):
                    self.client.submit(prepared, "替换背景")
                request.assert_not_called()

    def test_large_lossless_source_only_compresses_api_transfer_copy(self):
        prepared = self.prepared()
        # Trailing padding remains decodable; tests the byte boundary cheaply.
        original = prepared.source_png + b"\0" * (10 * 1024 * 1024)
        prepared = replace(prepared, source_png=original)
        reply = response({"output": {"task_id": "task-large"}})
        with patch.object(self.client.session, "request", return_value=reply) as request:
            self.client.submit(prepared, "纯白色摄影棚")
        transfer = request.call_args.kwargs["json"]["input"]["base_image_url"]
        self.assertTrue(transfer.startswith("data:image/jpeg;base64,"))
        self.assertLessEqual(len(base64.b64decode(transfer.split(",", 1)[1])), 10 * 1024 * 1024)
        self.assertIs(prepared.source_png, original)

    def test_query_preserves_task_failure_for_workflow_and_request_id(self):
        reply = response({
            "request_id": "request-456",
            "output": {"task_id": "task-123", "task_status": "FAILED",
                       "code": "InvalidParameter", "message": "bad image"},
        })
        with patch.object(self.client.session, "request", return_value=reply) as request:
            output = self.client.query("task-123")
        self.assertEqual(request.call_args.args, ("GET", "https://dashscope.aliyuncs.com/api/v1/tasks/task-123"))
        self.assertEqual(output["request_id"], "request-456")
        self.assertEqual(output["task_status"], "FAILED")
        error = self.client.task_error(output)
        self.assertTrue(error.terminal)
        self.assertEqual(error.code, "InvalidParameter")
        self.assertEqual(error.request_id, "request-456")

    def test_http_errors_have_actionable_messages_and_redact_key(self):
        for status, code, hint in [(401, "InvalidApiKey", "鉴权失败"),
                                   (429, "Throttling", "并发超限")]:
            with self.subTest(status=status):
                reply = response({"code": code, "request_id": "req-1",
                                  "message": f"Rejected {self.settings.api_key}"}, status)
                with patch.object(self.client.session, "request", return_value=reply):
                    with self.assertRaises(ApiError) as caught:
                        self.client.query("task-1")
                error = caught.exception
                self.assertIn(hint, str(error))
                self.assertNotIn(self.settings.api_key, str(error))
                self.assertEqual(error.code, code)
                self.assertEqual(error.request_id, "req-1")

    def test_top_level_error_even_with_http_200_is_not_success(self):
        reply = response({"code": "InvalidParameter", "message": "wrong model"})
        with patch.object(self.client.session, "request", return_value=reply):
            with self.assertRaises(ApiError) as caught:
                self.client.query("task-1")
        self.assertEqual(caught.exception.code, "InvalidParameter")

    def test_non_json_response_is_clear_and_does_not_echo_response_body(self):
        reply = response(status=502)
        reply.json.side_effect = ValueError(self.settings.api_key)
        with patch.object(self.client.session, "request", return_value=reply):
            with self.assertRaises(ApiError) as caught:
                self.client.query("task-1")
        self.assertIn("非 JSON", str(caught.exception))
        self.assertIn("502", str(caught.exception))
        self.assertNotIn(self.settings.api_key, str(caught.exception))

    def test_submit_timeout_does_not_automatically_retry_post(self):
        with patch.object(self.client.session, "request", side_effect=requests.Timeout) as request:
            with self.assertRaises(ApiError) as caught:
                self.client.submit(self.prepared(), "编辑背景")
        request.assert_called_once()
        self.assertIn("云端可能已受理", str(caught.exception))

    def test_nested_task_messages_are_redacted_before_result_errors(self):
        reply = response({"request_id": "req-1", "output": {
            "task_status": "SUCCEEDED",
            "results": [{"code": "ImageError", "message": self.settings.api_key}],
        }})
        with patch.object(self.client.session, "request", return_value=reply):
            output = self.client.query("task-1")
        with self.assertRaises(ApiError) as caught:
            result_url(output)
        self.assertNotIn(self.settings.api_key, str(caught.exception))
        self.assertEqual(caught.exception.code, "ImageError")
        self.assertTrue(caught.exception.terminal)
        self.assertEqual(result_url({"results": [{"code": "Failed"}, {"url": "https://example.com/image.png"}]}),
                         "https://example.com/image.png")

    def test_download_has_no_authorization_and_converts_jpeg_to_png(self):
        source = BytesIO()
        Image.new("RGB", (16, 16), "red").save(source, format="JPEG")
        with patch.object(self.client.session, "request", return_value=response(content=source.getvalue())) as request:
            result = self.client.download("https://example.com/result.jpg")
        kwargs = request.call_args.kwargs
        self.assertNotIn("Authorization", kwargs.get("headers", {}))
        self.assertNotIn("Authorization", self.client.session.headers)
        self.assertIsNone(self.client.session.auth)
        with Image.open(BytesIO(result)) as decoded:
            self.assertEqual(decoded.format, "PNG")
            self.assertEqual(decoded.size, (16, 16))
    def test_expired_result_download_is_terminal_but_transport_failure_is_resumable(self):
        for status in (403, 404, 410):
            with self.subTest(status=status), patch.object(self.client.session, "get", return_value=response(status=status)):
                with self.assertRaises(ApiError) as caught:
                    self.client.download("https://example.com/result.png")
                self.assertTrue(caught.exception.terminal)
                self.assertEqual(caught.exception.code, "ResultExpired")
        with patch.object(self.client.session, "get", side_effect=requests.Timeout):
            with self.assertRaises(ApiError) as caught:
                self.client.download("https://example.com/result.png")
            self.assertFalse(caught.exception.terminal)



class ConfigurationTests(unittest.TestCase):
    @patch("config.load_dotenv")
    def test_missing_key_and_nonfinite_intervals_are_rejected(self, _):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "API Key"):
                Settings.from_env()
        for value in ("nan", "inf", "0", "-1", "invalid"):
            with self.subTest(value=value), patch.dict(os.environ, {
                "DASHSCOPE_API_KEY": "test-secret", "POLL_INTERVAL_SECONDS": value,
            }, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env()

    @patch("config.load_dotenv")
    def test_model_configurable_and_key_hidden_in_repr(self, _):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-secret", "DASHSCOPE_MODEL": "wanx2.1-imageedit"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.model, "wanx2.1-imageedit")
        self.assertNotIn("test-secret", repr(settings))
    @patch("config.load_dotenv")
    def test_unsupported_models_and_untrusted_endpoints_rejected(self, _):
        for field, value in [("DASHSCOPE_MODEL", "wan2.7-image-pro"),
                             ("DASHSCOPE_BASE_URL", "https://example.com"),
                             ("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com.evil.test"),
                             ("DASHSCOPE_BASE_URL", "http://dashscope.aliyuncs.com"),
                             ("DASHSCOPE_BASE_URL", "https://user@dashscope.aliyuncs.com"),
                             ("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")]:
            with self.subTest(value=value), patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-secret", field: value}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env()
        self.assertEqual(Settings(api_key="test", base_url="https://workspace-123.cn-beijing.maas.aliyuncs.com").model,
                         "wanx2.1-imageedit")



if __name__ == "__main__":
    unittest.main()
