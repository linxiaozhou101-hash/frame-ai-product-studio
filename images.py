"""Keep the upload for preview and fit a separate image to Wanx's input limits."""

from dataclasses import dataclass, field
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

MIN_SIDE = 512
MAX_SIDE = 4096
MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ProductImage:
    data: bytes = field(repr=False)
    mime: str
    width: int
    height: int
    original_data: bytes | None = field(default=None, repr=False)
    original_width: int | None = None
    original_height: int | None = None
    adjustment: str = ""

    @property
    def preview_data(self) -> bytes:
        return self.original_data if self.original_data is not None else self.data


def _encode(image: Image.Image, fmt: str) -> tuple[bytes, str, bool]:
    buffer = BytesIO()
    image.save(buffer, format=fmt, **({"quality": 95, "subsampling": 0} if fmt == "JPEG" else {}))
    if buffer.tell() <= MAX_BYTES:
        return buffer.getvalue(), "image/jpeg" if fmt == "JPEG" else "image/png", False

    # Resampling can make a previously small PNG much larger. Keep the API's
    # byte limit even after resizing; JPEG fallback preserves geometry.
    rgb = Image.new("RGB", image.size, "#f5f5f5")
    if image.mode == "RGBA":
        rgb.paste(image, mask=image.getchannel("A"))
    else:
        rgb.paste(image.convert("RGB"))
    for quality in (90, 80, 70, 60):
        buffer = BytesIO()
        rgb.save(buffer, format="JPEG", quality=quality)
        if buffer.tell() <= MAX_BYTES:
            return buffer.getvalue(), "image/jpeg", True
    raise ValueError("图片处理后仍超过 10 MB，请降低图片质量后重试。")


def read_product_image(data: bytes) -> ProductImage:
    if len(data) > MAX_BYTES:
        raise ValueError("图片不能超过 10 MB，请压缩后重新上传。")
    try:
        with Image.open(BytesIO(data)) as image:
            fmt = image.format
            if fmt not in {"JPEG", "PNG"}:
                raise ValueError("请选择真正的 JPG 或 PNG 图片。")
            image.verify()
        with Image.open(BytesIO(data)) as source:
            orientation = source.getexif().get(274, 1)
            image = ImageOps.exif_transpose(source)
            image.load()
        width, height = image.size
        original_size = (width, height)
        scale = min(max(1.0, MIN_SIDE / min(width, height)), MAX_SIDE / max(width, height))
        resized_size = tuple(max(1, round(side * scale)) for side in original_size)
        canvas_size = tuple(max(MIN_SIDE, side) for side in resized_size)
        notes = []
        if orientation in range(2, 9):
            notes.append("已校正照片方向")
        if canvas_size == resized_size == original_size and not notes:
            return ProductImage(data, "image/jpeg" if fmt == "JPEG" else "image/png", width, height,
                                data, width, height)

        has_alpha = "A" in image.getbands() or "transparency" in image.info
        image = image.convert("RGBA" if has_alpha else "RGB")
        if resized_size != original_size:
            image = image.resize(resized_size, Image.Resampling.LANCZOS)
            notes.append("已等比放大" if scale > 1 else "已等比缩小")
        if canvas_size != resized_size:
            canvas = Image.new(image.mode, canvas_size, "#f5f5f5")
            canvas.paste(image, ((canvas.width - image.width) // 2, (canvas.height - image.height) // 2))
            image = canvas
            notes.append("已补边，保留完整商品")
        encoded, mime, compressed = _encode(image, fmt)
        if compressed:
            notes.append("已压缩为 JPG")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("无法读取这张图片，文件可能已损坏。请重新导出为 JPG 或 PNG。") from exc
    return ProductImage(encoded, mime, image.width, image.height, data, width, height,
                        "，".join(notes) + f"；生成用图 {image.width} × {image.height} px。")
