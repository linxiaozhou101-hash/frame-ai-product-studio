"""Server-side configuration. Credentials never enter Streamlit widgets."""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

SUPPORTED_MODELS = {"wanx2.1-imageedit"}


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    model: str = "wanx2.1-imageedit"
    base_url: str = "https://dashscope.aliyuncs.com"
    poll_interval_seconds: float = 2
    task_timeout_seconds: float = 180
    request_timeout_seconds: float = 30

    def __post_init__(self):
        if self.model not in SUPPORTED_MODELS:
            raise ValueError("当前商品保护流程只支持 DASHSCOPE_MODEL=wanx2.1-imageedit。"
                             "其他模型使用不同协议，不能直接替换模型名。")
        url = urlparse(self.base_url)
        domain = url.hostname or ""
        allowed = domain == "dashscope.aliyuncs.com" or re.fullmatch(
            r"[a-z0-9][a-z0-9-]*\.cn-beijing\.maas\.aliyuncs\.com", domain
        )
        if (url.scheme != "https" or not allowed or url.path or url.query or
                url.fragment or url.username or url.password or url.netloc != domain):
            raise ValueError("DASHSCOPE_BASE_URL 必须是百炼北京地域 HTTPS 官方域名，"
                             "例如 https://dashscope.aliyuncs.com；不要添加路径或端口。")

    @classmethod
    def from_env(cls):
        load_dotenv(Path(__file__).parent / ".env", override=False)
        key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not key:
            raise ValueError("尚未配置 API Key。请在本地 .env 或环境变量中设置 DASHSCOPE_API_KEY，再重启应用。")
        model = os.getenv("DASHSCOPE_MODEL", "wanx2.1-imageedit").strip()
        if not model:
            raise ValueError("DASHSCOPE_MODEL 不能为空；默认使用 wanx2.1-imageedit。")
        base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com").strip().rstrip("/")
        def seconds(name, default):
            try:
                value = float(os.getenv(name, str(default)))
            except ValueError:
                raise ValueError(f"{name} 必须是大于 0 的秒数。") from None
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是大于 0 的有限秒数。")
            return value

        return cls(key, model, base_url,
                   seconds("POLL_INTERVAL_SECONDS", 2),
                   seconds("TASK_TIMEOUT_SECONDS", 180),
                   seconds("REQUEST_TIMEOUT_SECONDS", 30))
