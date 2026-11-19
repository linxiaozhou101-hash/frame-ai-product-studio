"""Local foreground extraction and deterministic product-pixel preservation.

The app's alpha mask is white for the product; the API mask is its binary
inverse, because Wan's pure white pixels select the region to edit.
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from threading import Lock

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from images import MAX_BYTES, MAX_SIDE, MIN_SIDE, ProductImage


@dataclass(frozen=True)
class PreparedProduct:
    source_png: bytes = field(repr=False)
    alpha_png: bytes = field(repr=False)
    mask_png: bytes = field(repr=False)
    width: int
    height: int
    method: str
    foreground_fraction: float


_MODEL_LOCK = Lock()


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _open(data: bytes) -> Image.Image:
    with Image.open(BytesIO(data)) as image:
        result = ImageOps.exif_transpose(image)
        result.load()
        return result.copy()


def _fitting(original_size: tuple[int, int]):
    """Mirror images.read_product_image, including centered padding rounding."""
    width, height = original_size
    scale = min(max(1.0, MIN_SIDE / min(width, height)), MAX_SIDE / max(width, height))
    resized_size = tuple(max(1, round(side * scale)) for side in original_size)
    canvas_size = tuple(max(MIN_SIDE, side) for side in resized_size)
    offset = tuple((canvas - resized) // 2 for canvas, resized in zip(canvas_size, resized_size))
    return resized_size, canvas_size, offset


def _fit_mask(alpha: Image.Image, product: ProductImage, original_size: tuple[int, int]) -> Image.Image:
    resized_size, canvas_size, offset = _fitting(original_size)
    if canvas_size != (product.width, product.height):
        raise ValueError("图片预处理尺寸不一致，请重新上传商品照片。")
    if alpha.size != original_size:
        raise ValueError("商品蒙版尺寸必须与原图校正方向后的尺寸一致，请重新导出蒙版。")
    if alpha.size != resized_size:
        alpha = alpha.resize(resized_size, Image.Resampling.LANCZOS)
    canvas = Image.new("L", canvas_size, 0)
    canvas.paste(alpha, offset)
    return canvas


@lru_cache(maxsize=1)
def _session(model_dir: str):
    # The explicit local model prevents rembg's configurable cloud backends.
    from rembg import new_session

    return new_session("isnet-general-use", providers=["CPUExecutionProvider"])


def _automatic_alpha(image: Image.Image) -> Image.Image:
    cache_root = Path(__file__).resolve().parent / "work" / "models"
    os.environ.setdefault("U2NET_HOME", str(cache_root))
    os.environ.setdefault("NUMBA_CACHE_DIR", str(cache_root.parent / "numba"))
    model_dir = Path(os.environ["U2NET_HOME"]).expanduser()
    try:
        model_dir.mkdir(parents=True, exist_ok=True)
        from rembg import remove

        with _MODEL_LOCK:
            output = remove(image.convert("RGB"), session=_session(str(model_dir)),
                            only_mask=True, post_process_mask=False)
        return (_open(output) if isinstance(output, bytes) else output).convert("L")
    except ImportError:
        raise ValueError(
            "缺少本地商品识别依赖。请使用 Python 3.11–3.13 安装 requirements.txt，"
            "或上传白色商品、黑色背景的 PNG 蒙版。"
        ) from None
    except Exception:
        raise ValueError(
            "本地商品识别未完成。首次使用需要下载 isnet-general-use 模型，请检查网络和模型缓存目录；"
            "也可上传商品蒙版后继续。为保护商品，本次不会改用整图重绘。"
        ) from None


def prepare_product(product: ProductImage, mask_data: bytes | None = None) -> PreparedProduct:
    """Extract once per upload; reusable on style changes and failed retries.

Manual masks use the *visually oriented original* dimensions. Their alpha
channel is ignored: black/white luminance is the documented input convention.
"""
    try:
        original = _open(product.preview_data)
        source = _open(product.data).convert("RGB")
        if source.size != (product.width, product.height):
            raise ValueError("商品图尺寸与预处理信息不一致，请重新上传。")
        if mask_data is not None:
            if len(mask_data) > MAX_BYTES:
                raise ValueError("商品蒙版不能超过 10 MB。")
            with Image.open(BytesIO(mask_data)) as uploaded:
                if uploaded.format != "PNG":
                    raise ValueError("商品蒙版必须是 PNG 格式。")
                uploaded.verify()
            alpha = _fit_mask(_open(mask_data).convert("L"), product, original.size)
            method = "手动蒙版"
        elif ("A" in original.getbands() or "transparency" in original.info) and \
                original.convert("RGBA").getchannel("A").getextrema()[0] < 255:
            alpha = _fit_mask(original.convert("RGBA").getchannel("A"), product, original.size)
            method = "PNG 透明通道"
        else:
            alpha = _automatic_alpha(source)
            if alpha.size != source.size:
                raise ValueError("本地识别返回了不匹配的蒙版尺寸，请重新识别。")
            # Never treat padding added by images.py as a part of the product.
            resized_size, _, offset = _fitting(original.size)
            bounds = (offset[0], offset[1], offset[0] + resized_size[0], offset[1] + resized_size[1])
            limited = Image.new("L", source.size, 0)
            limited.paste(alpha.crop(bounds), offset)
            alpha = limited
            method = "本地自动识别"

        # Snap the opaque core to the exact source RGB. Keep a narrow soft edge
        # instead of globally blurring, which can damage text and thin handles.
        alpha = alpha.point(lambda value: 255 if value >= 250 else (0 if value <= 5 else value))
        histogram = alpha.histogram()
        total = source.width * source.height
        foreground_pixels = total - histogram[0]
        if not foreground_pixels or not histogram[0] or not sum(histogram[128:]):
            raise ValueError("未识别出有效的商品与背景分区，请上传白色商品、黑色背景的修正蒙版。")
        # No dilation: expanding would preserve a ring of the original tabletop.
        # Even soft/thin product edges receive API protection; final alpha blends
        # their source pixels while fully transparent handle holes are editable.
        api_mask = alpha.point(lambda value: 0 if value > 0 else 255).convert("RGB")
        return PreparedProduct(_png(source), _png(alpha), _png(api_mask),
                               source.width, source.height, method, foreground_pixels / total)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("无法读取商品照片或蒙版，请重新导出为有效的 JPG / PNG 文件。") from None


def _clean_generated_edges(background: Image.Image, alpha: Image.Image) -> Image.Image:
    """Remove the model's product/edge ghosts before restoring the real product.

An inpainted interior is hidden by the original foreground. Only a narrow ring
outside its silhouette remains visible. All pixels outside that ring remain
untouched, including the generated contact shadow below the product's base.
"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise ValueError("缺少结果边缘处理依赖，请重新安装 requirements.txt 后重试获取结果。") from None

    radius = max(4, min(32, round(min(background.size) * 8 / 512)))
    foreground = (np.asarray(alpha) > 0).astype("uint8") * 255
    ring_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
    cleanup = cv2.dilate(foreground, ring_kernel)
    bounds = alpha.getbbox()
    if bounds is None:
        raise ValueError("商品保护蒙版为空，请重新识别商品轮廓。")
    # Keep the external shadow below the lowest point instead of erasing a
    # complete eight-pixel band underneath it, which makes products float.
    cleanup[bounds[3] + max(1, radius // 8):] = 0
    try:
        cleaned = cv2.inpaint(np.asarray(background), cleanup, 3, cv2.INPAINT_TELEA)
    except cv2.error:
        raise ValueError("商品边缘处理未完成，请重试获取结果或修正商品蒙版。") from None
    return Image.fromarray(cleaned)


def compose_result(prepared: PreparedProduct, generated_bytes: bytes) -> bytes:
    """Clean background edge ghosts, then restore the input's product pixels."""
    try:
        background = _open(generated_bytes).convert("RGB")
        source = _open(prepared.source_png).convert("RGB")
        alpha = _open(prepared.alpha_png).convert("L")
        if source.size != alpha.size or source.size != (prepared.width, prepared.height):
            raise ValueError("商品保护数据尺寸不一致，请重新上传照片并识别轮廓。")
        if background.size != source.size:
            background = background.resize(source.size, Image.Resampling.LANCZOS)
        background = _clean_generated_edges(background, alpha)
        return _png(Image.composite(source, background, alpha))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("生成结果不是有效图片，无法合成；请重试获取结果。") from None


def preview_cutout(prepared: PreparedProduct) -> bytes:
    """Show source pixels over a checkerboard, without invoking a model."""
    source = _open(prepared.source_png).convert("RGB")
    alpha = _open(prepared.alpha_png).convert("L")
    checker = Image.new("RGB", source.size, "#eeeeee")
    draw = ImageDraw.Draw(checker)
    tile = max(12, min(source.size) // 24)
    for y in range(0, source.height, tile):
        for x in range(0, source.width, tile):
            if (x // tile + y // tile) % 2:
                draw.rectangle((x, y, x + tile - 1, y + tile - 1), fill="#d6d6d6")
    return _png(Image.composite(source, checker, alpha))
