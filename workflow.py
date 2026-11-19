"""Session-scoped job state, including resumable polling and result download."""

import time
from dataclasses import dataclass, field
from typing import Callable

from bailian import ApiError, BailianClient, result_url
from images import ProductImage
from prompts import build_prompt
from protection import PreparedProduct, compose_result


@dataclass
class Job:
    image: ProductImage
    style: str
    task_id: str = ""
    url: str = ""
    result: bytes | None = field(default=None, repr=False)
    raw_result: bytes | None = field(default=None, repr=False)
    prepared: PreparedProduct | None = field(default=None, repr=False)
    active: bool = True
    elapsed: float = 0
    error: str = ""
    error_code: str = ""
    request_id: str = ""
    terminal_failure: bool = False
    phase: str = "准备提交"
    task_status: str = ""
    model: str = ""
    base_url: str = ""
    submitted_prompt: str = ""

    def retry(self) -> None:
        # Transient query/download errors keep the paid task and its result URL.
        if self.terminal_failure:
            self.task_id = ""
            self.url = ""
            self.task_status = ""
            self.raw_result = None
            self.result = None
            self.base_url = ""
        self.error = self.error_code = self.request_id = ""
        self.terminal_failure = False
        self.active = True


def run_job(
    job: Job,
    client: BailianClient,
    *,
    poll_interval: float,
    timeout: float,
    on_update: Callable[[str, float], None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    started = clock()

    def update(phase: str) -> None:
        job.phase = phase
        on_update(phase, job.elapsed + clock() - started)

    try:
        if job.prepared is None:
            raise ApiError("请先识别商品轮廓，确认完整后再生成。", code="MissingProtection")
        # A resumed paid task must remain on the same provider endpoint/model.
        if job.base_url and job.base_url != client.settings.base_url:
            raise ApiError("服务地址已改变，请恢复提交任务时的 DASHSCOPE_BASE_URL 后继续查询。",
                           code="ConfigurationChanged")
        if job.model and job.model != client.settings.model:
            raise ApiError("模型配置已改变，请恢复本任务的模型设置后继续。", code="ConfigurationChanged")
        if not job.task_id:
            update("正在提交图片与编辑指令")
            job.submitted_prompt = build_prompt(job.style)
            job.model = client.settings.model
            job.base_url = client.settings.base_url
            job.task_id = client.submit(job.prepared, job.submitted_prompt)

        while not job.url:
            if clock() - started >= timeout:
                raise ApiError("本次等待已超时，任务可能仍在云端运行。点击重试可继续查询原任务。", code="PollingTimeout")
            update("正在查询任务状态")
            output = client.query(job.task_id)
            status = output.get("task_status")
            job.task_status = status or ""
            job.request_id = output.get("request_id", "")
            if status == "SUCCEEDED":
                job.url = result_url(output)
                break
            if status in {"FAILED", "CANCELED", "UNKNOWN"}:
                raise client.task_error(output)
            if status not in {"PENDING", "RUNNING"}:
                raise ApiError("服务返回了无法识别的任务状态，请重试查询。", code="UnexpectedStatus")
            update("任务排队中" if status == "PENDING" else "正在生成新背景")
            sleep(min(poll_interval, max(0, timeout - (clock() - started))))

        update("生成完成，正在获取结果图片")
        if job.raw_result is None:
            job.raw_result = client.download(job.url)
        update("正在合成原商品，保留颜色与文字")
        try:
            job.result = compose_result(job.prepared, job.raw_result)
        except ValueError as exc:
            # Retain downloaded result; a local retry should never cost another generation.
            raise ApiError(str(exc), code="CompositionError") from None
        update("生成成功")
    except ApiError as exc:
        job.error = str(exc)
        job.error_code = exc.code
        job.request_id = exc.request_id
        job.terminal_failure = exc.terminal
        update("本次未完成")
    finally:
        job.elapsed += clock() - started
        job.active = False
