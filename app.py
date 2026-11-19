"""AI 商品图换背景助手 — run with `streamlit run app.py`."""

import hashlib
from io import BytesIO

import streamlit as st
from PIL import Image, ImageOps

from bailian import BailianClient
from config import Settings
from images import read_product_image
from prompts import STYLES, build_prompt
from ui import apply_styles, empty_image
from workflow import Job, run_job
from protection import prepare_product, preview_cutout

st.set_page_config(page_title="AI 商品图换背景助手", page_icon="◈", layout="wide")
apply_styles()

st.markdown('<div class="brand">FRAME / AI PRODUCT STUDIO</div>', unsafe_allow_html=True)
st.title("AI 商品图换背景助手")
st.markdown('<p class="subtitle">让商品成为主角。上传一张照片，为它换一个干净、自然的新场景。</p>', unsafe_allow_html=True)

try:
    settings = Settings.from_env()
    config_error = ""
except ValueError as exc:
    settings = None
    config_error = str(exc)

job = st.session_state.get("job")
busy = bool(job and job.active)
product = None


def start_generation(image, chosen_style, model, prepared):
    st.session_state.job = Job(image=image, style=chosen_style, model=model, prepared=prepared)


def retry_generation():
    st.session_state.job.retry()


with st.container(border=True):
    upload_col, style_col = st.columns([1.15, 1], gap="large")
    with upload_col:
        st.subheader("01  上传商品照片")
        uploaded = st.file_uploader("选择商品照片", type=["jpg", "jpeg", "png"], key="upload", label_visibility="collapsed", disabled=busy)
        st.caption("JPG / PNG · 不超过 10 MB · 自动适配图片尺寸")
        if uploaded is not None:
            try:
                product = read_product_image(uploaded.getvalue())
            except ValueError as exc:
                st.error(str(exc))
    with style_col:
        st.subheader("02  选择背景风格")
        style = st.radio("背景风格", list(STYLES), horizontal=True, key="style", label_visibility="collapsed", disabled=busy)
        st.caption(STYLES[style]["description"])
        st.caption("商品保护已开启 · 保留原商品像素，仅生成新背景")
        with st.expander("商品轮廓（自动识别不准时可替换）"):
            mask_upload = st.file_uploader("可选：上传商品蒙版 PNG（白色商品，黑色背景）",
                                           type=["png"], key=f"mask_upload_{hashlib.sha256(product.preview_data).hexdigest() if product else 'empty'}", disabled=busy)
            st.caption("蒙版与原图方向和尺寸一致。白色保留商品，黑色替换背景；杯把内的空隙也应为黑色。")
        mask_data = mask_upload.getvalue() if mask_upload else None
        signature = (hashlib.sha256(product.preview_data).hexdigest(), style,
                     hashlib.sha256(mask_data).hexdigest() if mask_data else "auto") if product else None
        if signature != st.session_state.get("input_signature"):
            st.session_state.input_signature = signature
            st.session_state.job = None
            job = None
        prepared_key = (hashlib.sha256(product.data).hexdigest(), signature[2]) if product else None
        if prepared_key != st.session_state.get("prepared_key"):
            st.session_state.prepared_key = prepared_key
            st.session_state.prepared = None
            st.session_state.preparation_error = ""
        if product and not st.session_state.get("prepared"):
            if st.button("识别商品轮廓" if not mask_data else "预览商品轮廓", key="prepare", disabled=busy):
                with st.spinner("正在识别商品，首次使用需要下载本地模型…"):
                    try:
                        st.session_state.prepared = prepare_product(product, mask_data=mask_data)
                        st.session_state.preparation_error = ""
                    except ValueError as exc:
                        st.session_state.preparation_error = str(exc)
        prepared = st.session_state.get("prepared")
        if st.session_state.get("preparation_error"):
            st.error(st.session_state.preparation_error)
        if prepared:
            with st.expander("已识别的商品 · 查看轮廓", expanded=not bool(job and job.result)):
                st.image(preview_cutout(prepared), width=220)
                st.caption("确认商品完整、没有残留桌面后生成。轮廓不准时请上传修正蒙版。")
        if job and job.error:
            label = "重试生成" if job.terminal_failure or not job.task_id else "重试 · 继续获取结果"
            st.button(label, type="primary", width="stretch", key="retry", on_click=retry_generation,
                      disabled=busy or settings is None or product is None or prepared is None)
        else:
            st.button("正在生成…" if busy else ("重新生成" if job and job.result else "生成"),
                      type="primary", width="stretch", key="generate", on_click=start_generation,
                      args=(product, style, settings.model if settings else "", prepared),
                      disabled=busy or settings is None or product is None or prepared is None)

if config_error:
    st.info(config_error)

status_slot = st.empty()
if job and job.error:
    status_slot.error(job.error)
    details = [f"累计处理耗时 {job.elapsed:.1f} 秒"]
    if job.error_code:
        details.append(f"错误码：{job.error_code}")
    if job.request_id:
        details.append(f"请求 ID：{job.request_id}")
    st.caption(" · ".join(details))
elif job and job.result:
    status_slot.success(f"生成完成 · {job.style} · 累计处理耗时 {job.elapsed:.1f} 秒")

left, right = st.columns(2, gap="large")
with left:
    with st.container(border=True):
        st.subheader("原图")
        if product:
            with Image.open(BytesIO(product.preview_data)) as original:
                st.image(ImageOps.exif_transpose(original), width="stretch")
            st.caption(f"原图 {product.original_width or product.width} × {product.original_height or product.height} px")
            if product.adjustment:
                st.caption(product.adjustment)
        else:
            empty_image("从一张商品照片开始", "上传后在这里预览原图")
with right:
    with st.container(border=True):
        st.subheader("生成结果")
        if job and job.result:
            st.image(job.result, width="stretch")
            st.download_button("下载结果 PNG", data=job.result,
                               file_name=f"product-{STYLES[job.style]['slug']}.png", mime="image/png",
                               width="stretch", on_click="ignore")
        elif busy:
            empty_image("正在为商品布置新场景", "原图与背景风格已保留，请稍候")
        elif job and job.error:
            empty_image("这次还没有完成", "图片和风格已保留，可直接重试")
        else:
            empty_image("新场景，即将呈现", "选择背景风格，点击「生成」")

with st.expander("查看本次编辑指令"):
    st.write(getattr(job, "submitted_prompt", "") or build_prompt(style))
    st.caption("商品实心区域使用原图像素；半透明边缘作融合。请检查轮廓、接触阴影与背景是否自然。")

if job and job.task_id:
    with st.expander("任务信息"):
        st.text(f"任务 ID：{job.task_id}")
        st.text(f"状态：{job.phase}")
        st.text(f"API 状态：{getattr(job, 'task_status', '') or '尚未查询'}")
        st.text(f"模型：{getattr(job, 'model', '') or '未记录'}")
        st.text("编辑方式：背景局部重绘 + 原商品像素合成")
        if job.request_id:
            st.text(f"请求 ID：{job.request_id}")

st.markdown('<div class="footnote">FRAME · 阿里云百炼图像编辑 &nbsp; / &nbsp; 为商品换景，让展示更简单。</div>', unsafe_allow_html=True)

if job and job.active and settings is not None:
    status = status_slot.status("正在准备生成…", expanded=True)
    detail = status.empty()

    def show_progress(phase, elapsed):
        status.update(label=f"{phase} · 已用时 {elapsed:.1f} 秒", state="running")
        detail.caption(f"任务 ID：{job.task_id}" if job.task_id else "正在提交商品照片…")

    with BailianClient(settings) as client:
        run_job(job, client, poll_interval=settings.poll_interval_seconds,
                timeout=settings.task_timeout_seconds, on_update=show_progress)
    status.update(label=job.phase, state="error" if job.error else "complete", expanded=False)
    st.rerun()
