"""Wanx 2.1 HTTP API, following the official asynchronous image-edit contract.

https://help.aliyun.com/zh/model-studio/wanx-image-edit-api-reference
"""

import base64
from io import BytesIO
from urllib.parse import quote, urlparse

import requests
from PIL import Image, UnidentifiedImageError

from config import Settings
from protection import PreparedProduct


class ApiError(Exception):
    def __init__(self, message, *, code="", request_id="", terminal=False):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.terminal = terminal


def result_url(output: dict) -> str:
    results = output.get("results") or []
    if not isinstance(results, list):
        results = []
    results = [item for item in results if isinstance(item, dict)]
    for result in results:
        if isinstance(result.get("url"), str) and result["url"]:
            return result["url"]
    failed = next((item for item in results if item.get("code")), {})
    raise ApiError(
        "任务已结束，但没有可用的结果图片。" + str(failed.get("message", "请重试生成。")),
        code=failed.get("code", "MissingResult"),
        request_id=output.get("request_id", ""), terminal=True,
    )


class BailianClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.session.close()

    def _safe(self, value):
        # The provider may echo request information in its error message.
        if isinstance(value, str):
            return value.replace(self.settings.api_key, "[已隐藏]") if self.settings.api_key else value
        if isinstance(value, dict):
            return {key: self._safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._safe(item) for item in value]
        return value

    def _error(self, data, status=0, terminal=False):
        code = str(data.get("code", f"HTTP{status}" if status else "TaskFailed"))
        hint = "百炼请求未完成。"
        if status in {401, 403} or "ApiKey" in code or "AccessDenied" in code:
            hint = "鉴权失败，请检查北京地域 API Key、模型权限和服务地址。"
        elif status == 429 or "Throttl" in code or "Limit" in code:
            hint = "请求频率或并发超限，请稍后重试。"
        elif "Balance" in code or "Arrearage" in code:
            hint = "账户额度或余额不足，请在百炼控制台检查。"
        elif "InvalidParameter" in code or status == 400:
            hint = "请求参数不符合模型要求，请检查图片及模型配置。"
        elif "DataInspection" in code:
            hint = "图片或指令未通过服务内容检查，请更换图片后重试。"
        elif status >= 500:
            hint = "百炼服务暂时不可用，请稍后重试。"
        message = str(data.get("message", ""))
        return ApiError(self._safe(hint + (f" 服务提示：{message}" if message else "")),
                        code=self._safe(code), request_id=self._safe(str(data.get("request_id", ""))),
                        terminal=terminal)

    def _request(self, method, path, payload=None):
        headers = {"Authorization": f"Bearer {self.settings.api_key}"}
        if method == "POST":
            headers.update({"Content-Type": "application/json", "X-DashScope-Async": "enable"})
        try:
            response = self.session.request(method, self.settings.base_url + path,
                                            headers=headers, json=payload,
                                            allow_redirects=False,
                                            timeout=self.settings.request_timeout_seconds)
        except requests.Timeout:
            hint = ("提交超时，尚未取得任务 ID；云端可能已受理，可先查看百炼调用记录再重试。"
                    if method == "POST" else "查询超时，请重试继续查询原任务。")
            raise ApiError(hint, code="RequestTimeout") from None
        except requests.RequestException:
            raise ApiError("无法连接百炼服务，请检查网络和服务地址后重试。", code="NetworkError") from None
        try:
            data = response.json()
        except ValueError:
            raise ApiError(f"百炼返回了非 JSON 响应（HTTP {response.status_code}），请检查服务地址或稍后重试。",
                           code="InvalidResponse") from None
        if not isinstance(data, dict):
            raise ApiError("百炼响应格式异常，请稍后重试。", code="InvalidResponse")
        data = self._safe(data)
        if not 200 <= response.status_code < 300 or data.get("code"):
            if method == "GET" and response.status_code in {404, 410}:
                raise ApiError("任务不存在或已过期，请点击重试生成新任务。",
                               code="TaskExpired", request_id=data.get("request_id", ""), terminal=True)
            raise self._error(data, response.status_code)
        output = data.get("output")
        if not isinstance(output, dict):
            raise ApiError("百炼响应缺少任务信息，请稍后重试。", code="InvalidResponse",
                           request_id=data.get("request_id", ""))
        return {**output, "request_id": data.get("request_id", "")}

    def submit(self, prepared: PreparedProduct, prompt: str) -> str:
        """Submit explicit background repaint; never silently fall back to whole-image edit."""
        try:
            with Image.open(BytesIO(prepared.source_png)) as source, Image.open(BytesIO(prepared.mask_png)) as mask:
                source.load()
                mask.load()
                if source.size != mask.size or not all(512 <= side <= 4096 for side in source.size):
                    raise ValueError("商品图与背景蒙版必须同尺寸，且宽高均在 512–4096 像素之间。")
                if mask.mode != "RGB":
                    raise ValueError("背景蒙版必须为 RGB 黑白图片。")
                colors = mask.getcolors(3)
                if not colors or {color for _, color in colors} != {(0, 0, 0), (255, 255, 255)}:
                    raise ValueError("背景蒙版须包含纯黑商品区和纯白背景区。请重新识别商品轮廓。")
            if len(prepared.mask_png) > 10 * 1024 * 1024:
                raise ValueError("背景蒙版超过 10 MB，请缩小图片后重试。")
            if not prompt or len(prompt) > 800:
                raise ValueError("编辑指令长度必须在 1–800 字符之间。")
        except (AttributeError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ApiError(str(exc) if isinstance(exc, ValueError) else "无法读取商品图或背景蒙版，请重新识别轮廓。",
                           code="InvalidMask") from None

        def data_url(data, mime="image/png"):
            return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

        # JPEG input can expand past the API limit when decoded into lossless PNG.
        # Compress the transfer copy only; final composition keeps source_png intact.
        transfer_data, transfer_mime = prepared.source_png, "image/png"
        if len(transfer_data) > 10 * 1024 * 1024:
            with Image.open(BytesIO(prepared.source_png)) as original:
                for quality in (95, 90, 85, 80, 70, 60):
                    buffer = BytesIO()
                    original.convert("RGB").save(buffer, format="JPEG", quality=quality, subsampling=0)
                    transfer_data = buffer.getvalue()
                    if len(transfer_data) <= 10 * 1024 * 1024:
                        transfer_mime = "image/jpeg"
                        break
                else:
                    raise ApiError("生成用图压缩后仍超过 10 MB，请缩小图片后重试。", code="ImageTooLarge")

        output = self._request("POST", "/api/v1/services/aigc/image2image/image-synthesis", {
            "model": self.settings.model,
            "input": {"function": "description_edit_with_mask", "prompt": prompt,
                      "base_image_url": data_url(transfer_data, transfer_mime),
                      "mask_image_url": data_url(prepared.mask_png)},
            "parameters": {"n": 1, "watermark": False},
        })
        if not output.get("task_id"):
            raise ApiError("提交响应缺少任务 ID，无法查询。请先检查百炼调用记录再重试。",
                           code="MissingTaskId", request_id=output.get("request_id", ""))
        return output["task_id"]

    def query(self, task_id: str) -> dict:
        return self._request("GET", f"/api/v1/tasks/{quote(task_id, safe='')}")

    def task_error(self, output: dict) -> ApiError:
        status = output.get("task_status")
        if status == "FAILED":
            return self._error(output, terminal=True)
        message = "任务已取消，请重试生成。" if status == "CANCELED" else "任务不存在或已过期，请重新生成。"
        return ApiError(message, code=status or "TaskFailed",
                        request_id=output.get("request_id", ""), terminal=True)

    def download(self, url: str) -> bytes:
        # Auth is per API request, never a Session default or forwarded to OSS.
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ApiError("服务返回的图片地址无效，请重试获取任务结果。", code="InvalidResultUrl")
        try:
            response = self.session.get(url, timeout=self.settings.request_timeout_seconds)
            if response.status_code in {403, 404, 410}:
                raise ApiError("结果图片地址已过期或不可访问，请点击重试生成新任务。",
                               code="ResultExpired", terminal=True)
            response.raise_for_status()
        except requests.Timeout:
            raise ApiError("图片获取超时。生成任务已保留，点击重试可继续下载。", code="DownloadTimeout") from None
        except requests.RequestException:
            raise ApiError("结果图片获取失败，请检查网络后重试。若任务已超过 24 小时，请更换风格后重新生成。",
                           code="DownloadError") from None
        try:
            with Image.open(BytesIO(response.content)) as image:
                image.load()
                result = BytesIO()
                image.convert("RGBA" if "A" in image.getbands() else "RGB").save(result, format="PNG")
                return result.getvalue()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            raise ApiError("返回的结果不是有效图片，请重试获取原任务结果。", code="InvalidResultImage") from None
